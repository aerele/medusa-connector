from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import (
	get_erpnext_item,
)
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe.utils import flt

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.product.services.utils import (
	apply_country,
	apply_dimension_fields,
	apply_variant_hsn_code,
	ensure_barcodes,
	save_item,
	sync_item_price,
	upsert_mapping,
)


class VariantService:
	"""Synchronise Medusa variants into ERPNext Item Variants."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def sync_variant(
		self,
		template_code: str,
		product_id: str,
		variant: dict,
	) -> str | None:
		variant_id = variant.get("medusa_variant_id")
		attributes = variant.get("attributes") or {}

		if not variant_id or not attributes:
			return None

		item = self._resolve_existing_variant(
			template_code,
			product_id,
			variant,
			attributes,
		)

		if item:
			self.update_variant_item(item, variant)
		else:
			item = create_variant(template_code, attributes)

			if variant.get("sku"):
				item.item_code = variant["sku"]

			self.apply_variant_fields(item, variant)
			save_item(item)
			sync_item_price(item.name, variant, self.settings)

		self.finish_variant_mapping(
			item,
			product_id,
			variant,
			template_code,
		)

		return item.name

	def _resolve_existing_variant(
		self,
		template_code: str,
		product_id: str,
		variant: dict,
		attributes: dict,
	):
		"""Resolve by Ecommerce Item mapping, else by template + attribute combination."""
		item = get_erpnext_item(
			MODULE_NAME,
			product_id,
			variant_id=variant.get("medusa_variant_id"),
			sku=variant.get("sku"),
		)

		if item:
			return item

		existing_code = get_variant(
			template_code,
			attributes,
		)

		if existing_code:
			return frappe.get_doc(
				"Item",
				existing_code,
			)

		return None

	def finish_variant_mapping(
		self,
		item,
		product_id: str,
		variant: dict,
		template_code: str,
	) -> None:
		"""Create or update the Ecommerce Item mapping for the variant."""
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant.get("medusa_variant_id"),
			sku=variant.get("sku"),
			variant_of=template_code,
			has_variants=0,
		)

	def update_variant_item(
		self,
		item,
		variant: dict,
	) -> None:
		"""Update an existing ERPNext variant and save only when needed."""

		changed = self.apply_variant_fields(
			item,
			variant,
		)

		if changed:
			save_item(item)

		# Price synchronization is independent from Item document changes.
		sync_item_price(
			item.name,
			variant,
			self.settings,
		)

	def apply_variant_fields(self, item, variant: dict) -> bool:
		"""Apply Medusa variant fields and return whether Item changed."""
		changed = False

		if variant.get("item_name"):
			item_name = variant["item_name"][:140]
			if item.item_name != item_name:
				item.item_name = item_name
				changed = True

		if "allow_backorder" in variant:
			allow_negative_stock = int(bool(variant.get("allow_backorder")))

			if item.allow_negative_stock != allow_negative_stock:
				item.allow_negative_stock = allow_negative_stock
				changed = True

		if variant.get("image") and item.image != variant["image"]:
			item.image = variant["image"]
			changed = True

		if variant.get("weight") is not None:
			weight = flt(variant["weight"])

			if flt(item.weight_per_unit) != weight:
				item.weight_per_unit = weight
				changed = True

			weight_uom = item.weight_uom or self.settings.get("default_stock_uom") or "Nos"

			if item.weight_uom != weight_uom:
				item.weight_uom = weight_uom
				changed = True

		if apply_dimension_fields(item, variant):
			changed = True

		if "disabled" in variant:
			disabled = int(variant.get("disabled") or 0)

			if item.disabled != disabled:
				item.disabled = disabled
				changed = True

		if "manage_inventory" in variant:
			is_stock_item = int(bool(variant.get("manage_inventory")))

			if item.is_stock_item != is_stock_item:
				item.is_stock_item = is_stock_item
				changed = True

		before_country = item.get("country_of_origin")

		apply_country(
			item,
			variant.get("origin_country"),
		)

		if item.get("country_of_origin") != before_country:
			changed = True

		if apply_variant_hsn_code(item, variant):
			changed = True

		if ensure_barcodes(
			item,
			barcode=variant.get("barcode"),
			ean=variant.get("ean"),
			upc=variant.get("upc"),
		):
			changed = True

		return changed
