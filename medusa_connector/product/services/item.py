# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
from __future__ import annotations

import frappe
from frappe.utils import cstr, flt
from frappe.utils.nestedset import get_root_of

from medusa_connector.constants import SETTING_DOCTYPE
from medusa_connector.product.services.utils import (
	apply_country,
	apply_dimension_fields,
	apply_hsn_code,
	ensure_barcodes,
	save_item,
	upsert_mapping,
)


class MasterService:
	"""Creates and reuses ERPNext master data (UOM, Item Group, Brand, Item Attribute)."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self._ensured_masters: set[tuple[str, str]] = set()

	def get_or_create(
		self, doctype: str, name: str, name_field: str, extra: dict | None = None
	) -> str | None:
		"""Return an existing master record or create it if missing.

		Resolved masters are memoized per instance so a bulk sync reusing one
		MasterService does not re-query the same UOM / Brand / Item Group for
		every product in the catalog.
		"""
		name = (name or "").strip()
		if not name:
			return None
		key = (doctype, name)
		if key in self._ensured_masters or frappe.db.exists(doctype, name):
			self._ensured_masters.add(key)
			return name
		doc = {"doctype": doctype, name_field: name}
		if extra:
			doc.update(extra)
		frappe.get_doc(doc).insert()
		self._ensured_masters.add(key)
		return name

	def ensure_stock_uom(self, uom: str | None) -> str:
		"""Use provided UOM, or settings default, or 'Nos' as fallback."""
		uom_value = uom or self.settings.default_stock_uom or "Nos"
		return self.get_or_create("UOM", uom_value, "uom_name")

	def ensure_item_group(self, mapped: dict) -> str:
		"""First Medusa category name, else mapped/settings default, else root
		(categories carry no parent, so groups are always top-level)."""
		categories = mapped.get("categories") or []
		for cat in categories:
			name = cat.get("name")
			if name:
				return self.ensure_group(name)
		name = mapped.get("item_group") or self.settings.get("item_group")
		if not name:
			name = get_root_of("Item Group")
		return self.ensure_group(name)

	def ensure_group(self, name: str) -> str:
		name = (name or "").strip() or get_root_of("Item Group")
		parent = get_root_of("Item Group")
		return self.get_or_create(
			"Item Group",
			name,
			"item_group_name",
			extra={"parent_item_group": parent, "is_group": 0},
		)

	def ensure_brand(self, brand: str | None) -> str | None:
		return self.get_or_create("Brand", brand, "brand")

	def sync_attributes(self, attributes: list[dict]) -> None:
		names = [a.get("name") for a in attributes if a.get("name")]
		existing_names = (
			{
				row.name
				for row in frappe.get_all("Item Attribute", filters={"name": ["in", names]}, fields=["name"])
			}
			if names
			else set()
		)

		for attribute in attributes:
			name = attribute.get("name")
			if not name:
				continue
			values = list(dict.fromkeys(v for v in (attribute.get("values") or []) if v))
			if not values:
				continue
			if name not in existing_names:
				used: set[str] = set()
				self.get_or_create(
					"Item Attribute",
					name,
					"attribute_name",
					extra={
						"item_attribute_values": [
							{
								"attribute_value": value,
								"abbr": self.unique_abbr(value, used),
							}
							for value in values
						]
					},
				)
				continue
			# Skip numeric attributes
			if frappe.db.get_value("Item Attribute", name, "numeric_values"):
				continue
			rows = frappe.get_all(
				"Item Attribute Value",
				filters={"parent": name},
				fields=["attribute_value", "abbr"],
			)
			existing_values = {row.attribute_value for row in rows}
			existing_abbrs = {row.abbr for row in rows}
			new_values = [value for value in values if value not in existing_values]
			if not new_values:
				continue
			item_attribute = frappe.get_doc("Item Attribute", name)
			for value in new_values:
				item_attribute.append(
					"item_attribute_values",
					{
						"attribute_value": value,
						"abbr": self.unique_abbr(value, existing_abbrs),
					},
				)
			item_attribute.save()

	@staticmethod
	def unique_abbr(value: str, used: set[str]) -> str:
		base = cstr(value)[:10] or "VAL"
		abbr = base
		idx = 1
		while abbr in used:
			suffix = str(idx)
			abbr = f"{base[: max(1, 10 - len(suffix))]}{suffix}"
			idx += 1
		used.add(abbr)
		return abbr


class ItemService:
	def __init__(self, settings=None, master: MasterService | None = None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self.master = master or MasterService(self.settings)

	def sync_template(self, mapped: dict, existing=None) -> tuple[str, str]:
		return self._sync_item(mapped, existing, has_variants=1)

	def sync_simple_item(self, mapped: dict, existing=None) -> tuple[str, str]:
		variant_id = mapped.get("medusa_variant_id")
		if not variant_id:
			frappe.throw(f"Medusa product {mapped['medusa_product_id']} has no variant id")
		return self._sync_item(mapped, existing, has_variants=0, variant_id=variant_id)

	def _sync_item(
		self, mapped: dict, existing, *, has_variants: int, variant_id: str | None = None
	) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		sku = None if has_variants else mapped.get("sku")
		item_code = mapped["item_code"] if has_variants else (sku or mapped["item_code"])
		if existing:
			item_name = existing if isinstance(existing, str) else existing.name
			item = frappe.get_doc("Item", item_name)
			action = "updated"
		elif frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			action = "updated"
		else:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": mapped.get("item_name"),
					"is_sales_item": 1,
					"is_stock_item": int(mapped.get("is_stock_item", 0)),
					"has_variants": has_variants,
				}
			)
			action = "created"
		self.apply_item_fields(item, mapped, has_variants=has_variants)
		save_item(item)
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=sku,
			has_variants=has_variants,
		)
		return item.name, action

	def apply_item_fields(self, item, mapped: dict, *, has_variants: int) -> None:
		self._apply_basic_fields(item, mapped, has_variants)
		self._apply_brand(item, mapped)
		self._apply_inventory_fields(item, mapped, has_variants=has_variants)
		self._apply_tax_fields(item, mapped, has_variants)
		self._apply_template_fields(item, mapped, has_variants)
		apply_dimension_fields(item, mapped)

	def _apply_basic_fields(self, item, mapped: dict, has_variants: int) -> None:
		item.item_name = mapped.get("item_name") or item.item_name
		item.description = mapped.get("description") or item.description
		item.item_group = mapped.get("item_group") or item.item_group or get_root_of("Item Group")
		item.stock_uom = self.master.ensure_stock_uom(mapped.get("stock_uom") or item.stock_uom)
		if mapped.get("image"):
			item.image = mapped["image"]
		if "disabled" in mapped:
			item.disabled = int(mapped.get("disabled") or 0)
		item.has_variants = has_variants
		item.is_sales_item = 1
		item.is_stock_item = int(mapped.get("is_stock_item", item.is_stock_item))

	def _apply_brand(self, item, mapped: dict) -> None:
		if mapped.get("brand"):
			item.brand = self.master.ensure_brand(mapped["brand"])

	def _apply_inventory_fields(self, item, mapped: dict, *, has_variants: int = 0) -> None:
		if "allow_negative_stock" in mapped:
			item.allow_negative_stock = int(mapped.get("allow_negative_stock") or 0)
		if mapped.get("weight") is not None:
			item.weight_per_unit = flt(mapped["weight"])
			item.weight_uom = item.weight_uom or item.stock_uom
		if not has_variants and (mapped.get("barcode") or mapped.get("ean") or mapped.get("upc")):
			ensure_barcodes(
				item,
				barcode=mapped.get("barcode"),
				ean=mapped.get("ean"),
				upc=mapped.get("upc"),
			)

	def _apply_tax_fields(self, item, mapped: dict, has_variants: int) -> None:
		apply_country(item, mapped.get("origin_country"))
		apply_hsn_code(
			item,
			mapped.get("gst_hsn_code"),
			required_if_india_compliance=not has_variants,
		)

	def _apply_template_fields(self, item, mapped: dict, has_variants: int) -> None:
		if not has_variants:
			return
		item.attributes = []
		for attribute in mapped.get("attributes") or []:
			name = attribute.get("name")
			if name:
				item.append("attributes", {"attribute": name})
