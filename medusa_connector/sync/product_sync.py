# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Synchronise mapped Medusa products into ERPNext Items + Medusa Item Mapping.

Uses ERPNext's standard Item Variant APIs (``create_variant`` / ``get_variant``)
instead of hand-rolled Item inserts for attribute-based variants.
"""

from __future__ import annotations

import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe.utils import cstr, now_datetime

from medusa_connector.medusa_connector.doctype.medusa_item_mapping.medusa_item_mapping import (
	get_erpnext_item,
	mark_orphaned,
	upsert_mapping,
)


class ProductSync:
	"""Inbound product synchroniser (Medusa → ERPNext)."""

	def __init__(self) -> None:
		self.created = 0
		self.updated = 0
		self.skipped = 0

	def sync(self, mapped_product: dict, *, force: bool = False) -> dict:
		"""Create/update the template, attributes, variants, and mappings.

		Returns ``{item_code, action, variant_codes}`` where action is
		``created`` / ``updated`` / ``skipped``.
		"""
		product_id = mapped_product["medusa_product_id"]
		existing = get_erpnext_item(product_id, has_variants=mapped_product.get("has_variants") or 0)

		self._sync_attributes(mapped_product.get("attributes") or [])

		if mapped_product.get("has_variants"):
			template_code, action = self._sync_template(mapped_product, existing)
			variant_codes = []
			for variant in mapped_product.get("variants") or []:
				code = self._sync_variant(template_code, product_id, variant, force=force)
				if code:
					variant_codes.append(code)
			return {"item_code": template_code, "action": action, "variant_codes": variant_codes}

		# Simple (non-template) product.
		item_code, action = self._sync_simple_item(mapped_product, existing)
		return {"item_code": item_code, "action": action, "variant_codes": []}

	def disable_product(self, product_id: str) -> str | None:
		"""Disable ERPNext items linked to a deleted Medusa product."""
		template = get_erpnext_item(product_id, has_variants=1)
		simple = get_erpnext_item(product_id, has_variants=0)
		disabled: list[str] = []

		for item in filter(None, [template, simple]):
			if not item.disabled:
				item.disabled = 1
				item.flags.from_medusa = True
				item.save(ignore_permissions=True)
			disabled.append(item.name)
			mark_orphaned(product_id)
			# Disable child variants via mapping.
			variant_maps = frappe.get_all(
				"Medusa Item Mapping",
				filters={"medusa_product_id": product_id, "has_variants": 0, "variant_of": item.name},
				fields=["erpnext_item_code", "medusa_variant_id"],
			)
			for row in variant_maps:
				if frappe.db.exists("Item", row.erpnext_item_code):
					v = frappe.get_doc("Item", row.erpnext_item_code)
					if not v.disabled:
						v.disabled = 1
						v.flags.from_medusa = True
						v.save(ignore_permissions=True)
				mark_orphaned(product_id, row.medusa_variant_id)
				disabled.append(row.erpnext_item_code)

		# Fallback: mappings without has_variants=1 template row.
		if not disabled:
			maps = frappe.get_all(
				"Medusa Item Mapping",
				filters={"medusa_product_id": product_id},
				fields=["erpnext_item_code", "medusa_variant_id"],
			)
			for row in maps:
				if frappe.db.exists("Item", row.erpnext_item_code):
					item = frappe.get_doc("Item", row.erpnext_item_code)
					if not item.disabled:
						item.disabled = 1
						item.flags.from_medusa = True
						item.save(ignore_permissions=True)
					disabled.append(item.name)
				mark_orphaned(product_id, row.medusa_variant_id)

		return disabled[0] if disabled else None

	# ------------------------------------------------------------------
	# Internals
	# ------------------------------------------------------------------
	def _sync_attributes(self, attributes: list[dict]) -> None:
		for attribute in attributes:
			name = attribute.get("name")
			if not name:
				continue
			values = list(dict.fromkeys(v for v in (attribute.get("values") or []) if v))
			if frappe.db.exists("Item Attribute", name):
				item_attribute = frappe.get_doc("Item Attribute", name)
				if item_attribute.numeric_values:
					continue
				existing_values = {row.attribute_value for row in item_attribute.item_attribute_values}
				existing_abbrs = {row.abbr for row in item_attribute.item_attribute_values}
				changed = False
				for value in values:
					if value in existing_values:
						continue
					item_attribute.append(
						"item_attribute_values",
						{
							"attribute_value": value,
							"abbr": self._unique_abbr(value, existing_abbrs),
						},
					)
					existing_values.add(value)
					changed = True
				if changed:
					item_attribute.save(ignore_permissions=True)
			else:
				used: set[str] = set()
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": name,
						"item_attribute_values": [
							{
								"attribute_value": value,
								"abbr": self._unique_abbr(value, used),
							}
							for value in values
						],
					}
				).insert(ignore_permissions=True)

	@staticmethod
	def _unique_abbr(value: str, used: set[str]) -> str:
		"""ERPNext requires unique abbreviations within an Item Attribute."""
		base = cstr(value)[:10] or "VAL"
		abbr = base
		idx = 1
		while abbr in used:
			suffix = str(idx)
			abbr = f"{base[: max(1, 10 - len(suffix))]}{suffix}"
			idx += 1
		used.add(abbr)
		return abbr

	def _sync_template(self, mapped: dict, existing) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		# Prefer existing mapping item_code; never rename items on re-sync.
		item_code = existing.name if existing else mapped["item_code"]
		# If another item already owns that code, keep mapping to it.
		if not existing and frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			action = "updated"
			self.updated += 1
		elif existing:
			item = existing
			action = "updated"
			self.updated += 1
		else:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": mapped["item_name"],
					"is_stock_item": 1,
					"is_sales_item": 1,
				}
			)
			action = "created"
			self.created += 1

		self._apply_item_fields(item, mapped, has_variants=1)
		item.flags.from_medusa = True
		if item.is_new():
			item.insert(ignore_permissions=True, ignore_mandatory=True)
		else:
			item.save(ignore_permissions=True)

		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			has_variants=1,
			status="Disabled" if item.disabled else "Active",
		)
		return item.name, action

	def _sync_simple_item(self, mapped: dict, existing) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		# SKU match for simple products without a mapping yet.
		if not existing and mapped.get("sku") and frappe.db.exists("Item", mapped["sku"]):
			existing = frappe.get_doc("Item", mapped["sku"])

		if existing:
			item = existing
			action = "updated"
			self.updated += 1
		else:
			item_code = mapped.get("sku") or mapped["item_code"]
			if frappe.db.exists("Item", item_code):
				item = frappe.get_doc("Item", item_code)
				action = "updated"
				self.updated += 1
			else:
				item = frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item_code,
						"item_name": mapped["item_name"],
						"is_stock_item": 1,
						"is_sales_item": 1,
					}
				)
				action = "created"
				self.created += 1

		self._apply_item_fields(item, mapped, has_variants=0)
		item.flags.from_medusa = True
		if item.is_new():
			item.insert(ignore_permissions=True, ignore_mandatory=True)
		else:
			item.save(ignore_permissions=True)

		# Simple products often still have a single Medusa variant id — store it when present.
		variant_id = None
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=mapped.get("sku"),
			has_variants=0,
			status="Disabled" if item.disabled else "Active",
		)
		return item.name, action

	def _sync_variant(self, template_code: str, product_id: str, variant: dict, *, force: bool) -> str | None:
		variant_id = variant.get("medusa_variant_id")
		attrs = variant.get("attributes") or {}
		if not attrs:
			return None

		# Prefer existing mapping / attribute match.
		mapped_item = get_erpnext_item(product_id, variant_id=variant_id, sku=variant.get("sku"))
		if mapped_item:
			self._update_variant_item(mapped_item, variant)
			upsert_mapping(
				erpnext_item_code=mapped_item.name,
				medusa_product_id=product_id,
				variant_id=variant_id,
				sku=variant.get("sku"),
				variant_of=template_code,
				has_variants=0,
				status="Disabled" if mapped_item.disabled else "Active",
			)
			self.updated += 1
			return mapped_item.name

		existing_code = get_variant(template_code, attrs)
		if existing_code:
			item = frappe.get_doc("Item", existing_code)
			self._update_variant_item(item, variant)
			upsert_mapping(
				erpnext_item_code=item.name,
				medusa_product_id=product_id,
				variant_id=variant_id,
				sku=variant.get("sku"),
				variant_of=template_code,
				has_variants=0,
				status="Disabled" if item.disabled else "Active",
			)
			self.updated += 1
			return item.name

		# Create via ERPNext standard API.
		variant_doc = create_variant(template_code, attrs)
		if variant.get("sku"):
			# Prefer SKU as item_code when free; otherwise keep generated code.
			if not frappe.db.exists("Item", variant["sku"]):
				variant_doc.item_code = variant["sku"]
		if variant.get("item_name"):
			variant_doc.item_name = variant["item_name"]
		if variant.get("image"):
			variant_doc.image = variant["image"]
		if variant.get("barcode"):
			variant_doc.append("barcodes", {"barcode": variant["barcode"]})
		variant_doc.flags.from_medusa = True
		variant_doc.insert(ignore_permissions=True, ignore_mandatory=True)

		upsert_mapping(
			erpnext_item_code=variant_doc.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=variant.get("sku"),
			variant_of=template_code,
			has_variants=0,
			status="Active",
		)
		self.created += 1
		return variant_doc.name

	def _update_variant_item(self, item, variant: dict) -> None:
		changed = False
		if variant.get("item_name") and item.item_name != variant["item_name"]:
			item.item_name = variant["item_name"]
			changed = True
		if variant.get("image") and item.image != variant["image"]:
			item.image = variant["image"]
			changed = True
		if variant.get("barcode"):
			existing_barcodes = {row.barcode for row in item.barcodes or []}
			if variant["barcode"] not in existing_barcodes:
				item.append("barcodes", {"barcode": variant["barcode"]})
				changed = True
		if changed:
			item.flags.from_medusa = True
			item.save(ignore_permissions=True)

	def _apply_item_fields(self, item, mapped: dict, *, has_variants: int) -> None:
		item.item_name = mapped.get("item_name") or item.item_name
		item.description = mapped.get("description") or item.description
		item.item_group = mapped.get("item_group") or item.item_group
		item.stock_uom = mapped.get("stock_uom") or item.stock_uom or "Nos"
		item.image = mapped.get("image") or item.image
		item.disabled = int(mapped.get("disabled") or 0)
		item.has_variants = int(has_variants)
		if has_variants:
			item.attributes = []
			for attribute in mapped.get("attributes") or []:
				if attribute.get("name"):
					item.append("attributes", {"attribute": attribute["name"]})
		warehouse = mapped.get("default_warehouse")
		if warehouse:
			if not item.item_defaults:
				from erpnext import get_default_company

				item.append(
					"item_defaults",
					{"company": get_default_company(), "default_warehouse": warehouse},
				)
			else:
				item.item_defaults[0].default_warehouse = warehouse
