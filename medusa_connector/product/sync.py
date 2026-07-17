# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Synchronise mapped Medusa products into ERPNext Items + Ecommerce Item.

Uses ERPNext's standard Item Variant APIs (``create_variant`` / ``get_variant``)
and Medusa Admin product fields documented at https://docs.medusajs.com/api/admin.
"""

from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import get_erpnext_item

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.product.services import (
	ItemService,
	MasterService,
	VariantService,
)


class ProductSync:
	"""Inbound product synchroniser (Medusa → ERPNext)."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self.master = MasterService(self.settings)
		self.item = ItemService(self.settings, master=self.master)
		self.variant = VariantService(self.settings)

	def sync(self, mapped_product: dict, *, force: bool = False) -> dict:
		"""Create/update template, variants, prices and mappings.

		Returns ``{item_code, action, variant_codes}``. ``force`` is a no-op
		kept for caller compatibility.
		"""
		from medusa_connector.utils.sync_guard import inbound_sync

		with inbound_sync():
			try:
				return self._sync_product(mapped_product)
			except frappe.DuplicateEntryError:
				# A concurrent webhook or the ERPNext → Medusa export can insert
				# the same Item between our exists-check and insert. Roll back and
				# retry — the second pass sees the Item and updates it instead.
				frappe.db.rollback()
				return self._sync_product(mapped_product)

	def _sync_product(self, mapped_product: dict) -> dict:
		product_id = mapped_product["medusa_product_id"]
		has_variants = int(mapped_product.get("has_variants") or 0)
		existing = self._get_existing_item(mapped_product, has_variants)

		self._sync_masters(mapped_product)

		if has_variants:
			item_code, action, variant_codes = self._sync_template_with_variants(
				mapped_product, existing, product_id
			)
		else:
			item_code, action = self.item.sync_simple_item(mapped_product, existing)
			variant_codes = []

		self.apply_tags(item_code, mapped_product.get("tags") or [])
		return {"item_code": item_code, "action": action, "variant_codes": variant_codes}

	def _sync_masters(self, mapped_product: dict) -> None:
		self.master.ensure_stock_uom(mapped_product.get("stock_uom"))
		self.master.sync_attributes(mapped_product.get("attributes") or [])
		self.master.ensure_item_group(mapped_product)
		self.master.ensure_brand(mapped_product.get("brand"))

	def _sync_template_with_variants(
		self,
		mapped_product: dict,
		existing,
		product_id: str,
	) -> tuple[str, str, list[str]]:
		template_code, action = self.item.sync_template(mapped_product, existing)
		variant_codes = [
			code
			for variant in mapped_product.get("variants") or []
			if (code := self.variant.sync_variant(template_code, product_id, variant))
		]
		return template_code, action, variant_codes

	def _get_existing_item(self, mapped_product: dict, has_variants: int):
		product_id = mapped_product["medusa_product_id"]
		return get_erpnext_item(
			MODULE_NAME,
			product_id,
			variant_id=mapped_product.get("medusa_variant_id"),
			sku=mapped_product.get("sku"),
			has_variants=has_variants,
		)

	def apply_tags(self, item_code: str, tags: list[str]) -> None:
		if not tags or not item_code:
			return
		try:
			from frappe.desk.doctype.tag.tag import add_tag

			for tag in filter(None, tags):
				add_tag(tag, "Item", item_code)
		except Exception:
			# Tag module optional / permission issues should not fail product sync.
			frappe.log_error(
				frappe.get_traceback(),
				f"Failed to apply tags to Item {item_code}",
			)
