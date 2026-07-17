# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Synchronise mapped Medusa products into ERPNext Items + Ecommerce Item.

Uses ERPNext's standard Item Variant APIs (``create_variant`` / ``get_variant``)
and Medusa Admin product fields documented at https://docs.medusajs.com/api/admin.
"""

from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import get_erpnext_item
from frappe.utils import flt

from medusa_connector.constants import DEFAULT_STOCK_UOM, MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.product.services import (
	HSNService,
	ItemService,
	MasterService,
	MetadataService,
	PersistenceService,
	PriceService,
	VariantService,
)


class ProductSync:
	"""Inbound product synchroniser (Medusa → ERPNext)."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self.master = MasterService(self.settings)
		self.item = ItemService(self.settings, master=self.master)
		self.variant = VariantService(self.settings)
		self.metadata = MetadataService(self.settings)
		self.price = PriceService(self.settings)
		self.hsn = HSNService(self.settings)
		self.persistence = PersistenceService(self.settings)

	def sync(self, mapped_product: dict, *, force: bool = False) -> dict:
		"""Create/update the template, attributes, variants, prices, and mappings.

		Returns ``{item_code, action, variant_codes}`` where action is
		``created`` / ``updated`` / ``skipped``.
		"""
		from medusa_connector.utils.sync_guard import inbound_sync

		with inbound_sync():
			product_id = mapped_product["medusa_product_id"]
			has_variants = int(mapped_product.get("has_variants") or 0)
			existing = self._get_existing_item(mapped_product, has_variants)

			self._sync_masters(mapped_product)

			if has_variants:
				item_code, action, variant_codes = self._sync_template_with_variants(
					mapped_product, existing, product_id, force=force
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
		*,
		force: bool,
	) -> tuple[str, str, list[str]]:
		template_code, action = self.item.sync_template(mapped_product, existing)
		variant_codes = [
			code
			for variant in mapped_product.get("variants") or []
			if (code := self.variant.sync_variant(template_code, product_id, variant, force=force))
		]
		return template_code, action, variant_codes

	def _get_existing_item(self, mapped_product: dict, has_variants: int):
		product_id = mapped_product["medusa_product_id"]
		return get_erpnext_item(
			MODULE_NAME,
			product_id,
			variant_id=mapped_product.get("variant_id") or mapped_product.get("medusa_variant_id"),
			sku=mapped_product.get("sku"),
			has_variants=has_variants,
		)

	# ------------------------------------------------------------------
	# Inventory Item webhook support
	# ------------------------------------------------------------------
	def _inventory_snapshot(self, item) -> dict[str, object]:
		return {
			"weight_per_unit": flt(item.weight_per_unit),
			"gst_hsn_code": item.get("gst_hsn_code"),
			"country_of_origin": item.get("country_of_origin"),
			"image": item.image,
			"item_name": item.item_name,
		}

	def apply_inventory_item_fields(self, item, inv: dict) -> bool:
		"""Apply Medusa Inventory Item fields onto an ERPNext Item.

		Used by ``inventory-item.*`` webhooks (Admin Inventory module). Inventory
		Item is the source of truth for ``hs_code``, weight, material,
		``origin_country``, and ``mid_code`` when those are edited in Medusa
		Inventory (not the Product catalog).

		Returns ``True`` when the Item document was mutated and should be saved.
		"""
		if not inv:
			return False

		before = self._inventory_snapshot(item)

		if inv.get("weight") is not None:
			item.weight_per_unit = flt(inv.get("weight"))
			item.weight_uom = (
				item.weight_uom
				or item.stock_uom
				or self.settings.get("default_stock_uom")
				or DEFAULT_STOCK_UOM
			)

		# HSN / HS code — Inventory Item.hs_code is authoritative for this event.
		if "hs_code" in inv:
			hs = str(inv.get("hs_code") or "").strip()

			if hs:
				self.hsn.apply_hsn_code(
					item,
					hs,
					required_if_india_compliance=False,
				)

		if inv.get("origin_country"):
			self.hsn.apply_country(item, inv.get("origin_country"))

		if inv.get("thumbnail"):
			item.image = inv["thumbnail"]

		title = (inv.get("title") or "").strip()
		if title and not item.get("has_variants"):
			new_name = title[:140]
			if new_name and item.item_name != new_name:
				item.item_name = new_name

		# Material, MID, L/W/H → real custom fields (no description parsing).
		# These live outside the native-field snapshot, so their own return
		# value has to be OR'd in — a snapshot diff alone would miss them.
		metadata_changed = self.metadata.apply_inventory_fields(item, inv)

		return metadata_changed or before != self._inventory_snapshot(item)

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
