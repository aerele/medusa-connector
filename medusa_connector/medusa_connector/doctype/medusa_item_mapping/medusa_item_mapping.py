# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa ↔ ERPNext item identity map (standalone Ecommerce Item equivalent)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime


class MedusaItemMapping(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		erpnext_item_code: DF.Link
		has_variants: DF.Check
		last_error: DF.SmallText | None
		last_sync_status: DF.Literal["Pending", "Success", "Failed"]
		last_synced_on: DF.Datetime | None
		medusa_product_id: DF.Data
		medusa_variant_id: DF.Data | None
		sku: DF.Data | None
		status: DF.Literal["Active", "Disabled", "Orphaned"]
		variant_of: DF.Link | None
	# end: auto-generated types

	def before_insert(self) -> None:
		self.check_unique_constraints()

	def validate(self) -> None:
		self.medusa_variant_id = cstr(self.medusa_variant_id)
		self.sku = cstr(self.sku) if not self.has_variants else None
		if self.has_variants:
			self.medusa_variant_id = ""

	def check_unique_constraints(self) -> None:
		filters = {
			"medusa_product_id": self.medusa_product_id,
			"medusa_variant_id": cstr(self.medusa_variant_id),
		}
		existing = frappe.db.exists("Medusa Item Mapping", filters)
		if existing and existing != self.name:
			frappe.throw(
				_("Medusa Item Mapping already exists for this product/variant."), frappe.DuplicateEntryError
			)

		if self.sku:
			sku_existing = frappe.db.exists("Medusa Item Mapping", {"sku": self.sku})
			if sku_existing and sku_existing != self.name:
				frappe.throw(
					_("Medusa Item Mapping already exists for SKU {0}.").format(self.sku),
					frappe.DuplicateEntryError,
				)


# ---------------------------------------------------------------------------
# Module-level helpers (Shopify Ecommerce Item style, Medusa-scoped)
# ---------------------------------------------------------------------------


def is_synced(medusa_product_id: str, variant_id: str | None = None, sku: str | None = None) -> bool:
	filters: dict = {"medusa_product_id": medusa_product_id, "medusa_variant_id": cstr(variant_id)}
	if frappe.db.exists("Medusa Item Mapping", filters):
		return True
	if sku:
		return bool(frappe.db.exists("Medusa Item Mapping", {"sku": sku}))
	return False


def get_erpnext_item_code(
	medusa_product_id: str,
	variant_id: str | None = None,
	has_variants: int | None = 0,
) -> str | None:
	filters: dict = {"medusa_product_id": medusa_product_id}
	if variant_id:
		filters["medusa_variant_id"] = cstr(variant_id)
	elif has_variants:
		filters["has_variants"] = 1
		filters["medusa_variant_id"] = ""
	else:
		filters["medusa_variant_id"] = cstr(variant_id)
	return frappe.db.get_value("Medusa Item Mapping", filters, "erpnext_item_code")


def get_erpnext_item(
	medusa_product_id: str,
	variant_id: str | None = None,
	sku: str | None = None,
	has_variants: int | None = 0,
):
	item_code = None
	if sku:
		item_code = frappe.db.get_value("Medusa Item Mapping", {"sku": sku}, "erpnext_item_code")
	if not item_code:
		item_code = get_erpnext_item_code(medusa_product_id, variant_id=variant_id, has_variants=has_variants)
	if item_code and frappe.db.exists("Item", item_code):
		return frappe.get_doc("Item", item_code)
	return None


def upsert_mapping(
	*,
	erpnext_item_code: str,
	medusa_product_id: str,
	variant_id: str | None = None,
	sku: str | None = None,
	variant_of: str | None = None,
	has_variants: int = 0,
	status: str = "Active",
	sync_status: str = "Success",
	error: str | None = None,
) -> str:
	"""Create or update a mapping row. Returns the mapping name."""
	filters = {
		"medusa_product_id": medusa_product_id,
		"medusa_variant_id": cstr(variant_id),
	}
	name = frappe.db.exists("Medusa Item Mapping", filters)
	values = {
		"erpnext_item_code": erpnext_item_code,
		"sku": cstr(sku) if not has_variants else None,
		"variant_of": cstr(variant_of) if variant_of else None,
		"has_variants": int(has_variants),
		"status": status,
		"last_synced_on": now_datetime(),
		"last_sync_status": sync_status,
		"last_error": error,
	}
	if name:
		doc = frappe.get_doc("Medusa Item Mapping", name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Medusa Item Mapping",
			"medusa_product_id": medusa_product_id,
			"medusa_variant_id": cstr(variant_id),
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def mark_orphaned(medusa_product_id: str, variant_id: str | None = None) -> None:
	filters = {
		"medusa_product_id": medusa_product_id,
		"medusa_variant_id": cstr(variant_id),
	}
	name = frappe.db.exists("Medusa Item Mapping", filters)
	if name:
		frappe.db.set_value(
			"Medusa Item Mapping",
			name,
			{"status": "Orphaned", "last_synced_on": now_datetime()},
		)


def get_mapping_health() -> dict:
	"""Detect duplicates, missing ERPNext items, and orphaned mappings."""
	all_maps = frappe.get_all(
		"Medusa Item Mapping",
		fields=[
			"name",
			"erpnext_item_code",
			"medusa_product_id",
			"medusa_variant_id",
			"sku",
			"status",
		],
	)
	missing_items: list[dict] = []
	orphaned: list[dict] = []
	seen_keys: dict[tuple, list[str]] = {}
	duplicates: list[dict] = []

	for row in all_maps:
		key = (row.medusa_product_id, cstr(row.medusa_variant_id))
		seen_keys.setdefault(key, []).append(row.name)
		if row.status == "Orphaned":
			orphaned.append(row)
		if not frappe.db.exists("Item", row.erpnext_item_code):
			missing_items.append(row)

	for key, names in seen_keys.items():
		if len(names) > 1:
			duplicates.append({"medusa_product_id": key[0], "medusa_variant_id": key[1], "names": names})

	return {
		"total": len(all_maps),
		"orphaned_count": len(orphaned),
		"missing_item_count": len(missing_items),
		"duplicate_count": len(duplicates),
		"orphaned": orphaned[:50],
		"missing_items": missing_items[:50],
		"duplicates": duplicates[:50],
	}
