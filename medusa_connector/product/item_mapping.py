# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa product identity helpers — backed by Ecommerce Core ``Ecommerce Item``.

Mapping model (Medusa always has a sellable Variant, even for single-variant products):

| Field | Sellable row (``has_variants=0``) | Template row (``has_variants=1``) |
|-------|-----------------------------------|-----------------------------------|
| ``integration_item_code`` | **Medusa Variant ID** | Medusa Product ID (product anchor) |
| ``variant_id`` | Medusa Variant ID | empty |
| ``sku`` | Medusa variant SKU / ERPNext item code | empty |
| ``variant_of`` | ERPNext template Item (if any) | empty |

``variant_of`` is a Link to **Item** in Ecommerce Core (not Medusa product id).
Medusa product id for a sellable row is resolved via the template Ecommerce Item
or Admin API when needed.
"""

from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item as ecom
from frappe.utils import cstr, now

from medusa_connector.constants import MODULE_NAME


def is_synced(
	medusa_product_id: str | None = None,
	variant_id: str | None = None,
	sku: str | None = None,
) -> bool:
	"""Return True if this Medusa product/variant is mapped in Ecommerce Item.

	- With ``variant_id``: look up by Variant ID (``integration_item_code`` or ``variant_id``).
	- With ``sku`` only: SKU lookup.
	- With ``medusa_product_id`` only: template row, any legacy product-id row, or
	  sellable rows resolved via ``is_product_synced`` helpers at the call site
	  that have variant data from the product payload.
	"""
	variant_id = cstr(variant_id) if variant_id else None
	if variant_id:
		if frappe.db.exists(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "integration_item_code": variant_id},
		):
			return True
		if frappe.db.exists(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "variant_id": variant_id},
		):
			return True
		return False

	if sku:
		return ecom._is_sku_synced(MODULE_NAME, sku)

	if medusa_product_id:
		return is_product_synced(medusa_product_id)
	return False


def is_product_synced(medusa_product_id: str, product: dict | None = None) -> bool:
	"""True if any mapping exists for this Medusa product.

	Prefers template row (``integration_item_code = product_id``, ``has_variants=1``).
	Falls back to legacy sellable rows keyed by product id, then variant ids from
	an optional product payload (list/import pages should pass the product dict).
	"""
	medusa_product_id = cstr(medusa_product_id)
	if not medusa_product_id:
		return False

	if frappe.db.exists(
		"Ecommerce Item",
		{
			"integration": MODULE_NAME,
			"integration_item_code": medusa_product_id,
			"has_variants": 1,
		},
	):
		return True

	# Legacy: simple/variant rows still keyed by product id
	if frappe.db.exists(
		"Ecommerce Item",
		{"integration": MODULE_NAME, "integration_item_code": medusa_product_id},
	):
		return True

	if product:
		for variant in product.get("variants") or []:
			if not isinstance(variant, dict):
				continue
			vid = cstr(variant.get("id") or "")
			if vid and is_synced(variant_id=vid):
				return True
	return False


def get_erpnext_item_code(
	medusa_product_id: str | None = None,
	variant_id: str | None = None,
	has_variants: int | None = 0,
) -> str | None:
	variant_id = cstr(variant_id) if variant_id else None

	if variant_id:
		code = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "integration_item_code": variant_id},
			"erpnext_item_code",
		)
		if code:
			return code
		return frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "variant_id": variant_id},
			"erpnext_item_code",
		)

	if medusa_product_id and has_variants:
		return ecom.get_erpnext_item_code(MODULE_NAME, medusa_product_id, has_variants=1)

	if medusa_product_id:
		# Simple product: prefer a sellable row with this product id (legacy),
		# then template, then first sellable under template.
		code = ecom.get_erpnext_item_code(MODULE_NAME, medusa_product_id, has_variants=0)
		if code:
			return code
		return ecom.get_erpnext_item_code(MODULE_NAME, medusa_product_id, has_variants=1)
	return None


def get_erpnext_item(
	medusa_product_id: str | None = None,
	variant_id: str | None = None,
	sku: str | None = None,
	has_variants: int | None = 0,
):
	if sku:
		item_code = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "sku": sku},
			"erpnext_item_code",
		)
		if item_code and frappe.db.exists("Item", item_code):
			return frappe.get_doc("Item", item_code)

	item_code = get_erpnext_item_code(
		medusa_product_id=medusa_product_id,
		variant_id=variant_id,
		has_variants=has_variants,
	)
	if item_code and frappe.db.exists("Item", item_code):
		return frappe.get_doc("Item", item_code)
	return None


def upsert_mapping(
	*,
	erpnext_item_code: str,
	medusa_product_id: str | None = None,
	variant_id: str | None = None,
	sku: str | None = None,
	variant_of: str | None = None,
	has_variants: int = 0,
	status: str = "Active",
	sync_status: str = "Success",
	error: str | None = None,
	medusa_variant_id: str | None = None,
) -> str:
	"""Create or update an Ecommerce Item row. Returns its name.

	Sellable rows (``has_variants=0``) require a Medusa Variant ID; it is stored in
	both ``integration_item_code`` and ``variant_id``.

	Template rows (``has_variants=1``) store the Medusa Product ID in
	``integration_item_code`` so product-level ops can find the product.
	"""
	_ = (status, sync_status, error)  # accepted for call-site compatibility

	has_variants = int(has_variants or 0)
	variant_id = cstr(medusa_variant_id or variant_id or "")
	medusa_product_id = cstr(medusa_product_id or "")
	sku = cstr(sku) if not has_variants else None
	variant_of = cstr(variant_of) if variant_of else ""

	if has_variants:
		if not medusa_product_id:
			frappe.throw("medusa_product_id is required for template Ecommerce Item mapping")
		integration_item_code = medusa_product_id
		variant_id = ""
		sku = None
		variant_of = ""
		filters: dict = {
			"integration": MODULE_NAME,
			"integration_item_code": integration_item_code,
			"has_variants": 1,
		}
	else:
		if not variant_id:
			frappe.throw(
				"Medusa Variant ID is required for sellable Ecommerce Item mapping "
				"(integration_item_code stores the Variant ID)."
			)
		integration_item_code = variant_id
		filters = {
			"integration": MODULE_NAME,
			"integration_item_code": integration_item_code,
		}

	name = frappe.db.exists("Ecommerce Item", filters)
	# Also match legacy sellable rows keyed by product id + variant_id.
	if not name and not has_variants and medusa_product_id and variant_id:
		name = frappe.db.exists(
			"Ecommerce Item",
			{
				"integration": MODULE_NAME,
				"integration_item_code": medusa_product_id,
				"variant_id": variant_id,
			},
		)
	if not name and not has_variants and variant_id:
		name = frappe.db.exists(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "variant_id": variant_id},
		)

	values = {
		"erpnext_item_code": erpnext_item_code,
		"integration_item_code": integration_item_code,
		"sku": sku,
		"variant_of": variant_of,
		"has_variants": has_variants,
		"variant_id": variant_id,
		"item_synced_on": now(),
	}

	if name:
		frappe.db.set_value("Ecommerce Item", name, values, update_modified=False)
		return name

	if sku:
		by_sku = frappe.db.exists("Ecommerce Item", {"integration": MODULE_NAME, "sku": sku})
		if by_sku:
			frappe.db.set_value("Ecommerce Item", by_sku, values, update_modified=False)
			return by_sku

	# Same ERPNext item may already map (re-key from product id → variant id).
	by_item = frappe.db.exists(
		"Ecommerce Item",
		{"integration": MODULE_NAME, "erpnext_item_code": erpnext_item_code},
	)
	if by_item:
		frappe.db.set_value("Ecommerce Item", by_item, values, update_modified=False)
		return by_item

	doc = frappe.get_doc(
		{
			"doctype": "Ecommerce Item",
			"integration": MODULE_NAME,
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def get_medusa_variant_id(row) -> str | None:
	"""Sellable Variant ID from an Ecommerce Item row (dict or object)."""
	if not row:
		return None
	if isinstance(row, dict):
		has_variants = cint_safe(row.get("has_variants"))
		vid = cstr(row.get("variant_id") or "")
		code = cstr(row.get("integration_item_code") or "")
	else:
		has_variants = cint_safe(row.has_variants)
		vid = cstr(getattr(row, "variant_id", None) or "")
		code = cstr(getattr(row, "integration_item_code", None) or "")
	if has_variants:
		return None
	return vid or code or None


def get_medusa_product_id_from_row(row, *, fetch_if_missing: bool = False) -> str | None:
	"""Resolve Medusa Product ID for an Ecommerce Item row.

	Template rows: ``integration_item_code``.
	Sellable multi-variant: template Ecommerce Item for ``variant_of`` ERPNext item.
	Sellable simple: optional Admin API fetch via Variant ID.
	"""
	if not row:
		return None
	if isinstance(row, dict):
		has_variants = cint_safe(row.get("has_variants"))
		code = cstr(row.get("integration_item_code") or "")
		variant_of = cstr(row.get("variant_of") or "")
		variant_id = get_medusa_variant_id(row)
	else:
		has_variants = cint_safe(row.has_variants)
		code = cstr(getattr(row, "integration_item_code", None) or "")
		variant_of = cstr(getattr(row, "variant_of", None) or "")
		variant_id = get_medusa_variant_id(row)

	if has_variants and code:
		return code

	if variant_of:
		pid = frappe.db.get_value(
			"Ecommerce Item",
			{
				"integration": MODULE_NAME,
				"erpnext_item_code": variant_of,
				"has_variants": 1,
			},
			"integration_item_code",
		)
		if pid:
			return pid

	# Legacy: sellable still keyed by product id
	if code.startswith("prod_"):
		return code

	if fetch_if_missing and variant_id:
		return fetch_product_id_for_variant(variant_id)
	return None


def fetch_product_id_for_variant(variant_id: str) -> str | None:
	"""Admin API: resolve product id for a variant."""
	if not variant_id:
		return None
	try:
		from medusa_connector.medusa.product import ProductService

		variant = ProductService().get_variant(variant_id) or {}
		return (
			cstr(
				variant.get("product_id")
				or (
					(variant.get("product") or {}).get("id")
					if isinstance(variant.get("product"), dict)
					else None
				)
				or ""
			)
			or None
		)
	except Exception:
		return None


def get_ecommerce_items_for_product(medusa_product_id: str) -> list[dict]:
	"""All Ecommerce Item rows for a Medusa product (template + sellables)."""
	medusa_product_id = cstr(medusa_product_id)
	if not medusa_product_id:
		return []

	rows = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME, "integration_item_code": medusa_product_id},
		fields=[
			"name",
			"erpnext_item_code",
			"integration_item_code",
			"variant_id",
			"variant_of",
			"has_variants",
			"sku",
		],
	)
	# Template + sellables linked via ERPNext variant_of → template map
	template = frappe.db.get_value(
		"Ecommerce Item",
		{
			"integration": MODULE_NAME,
			"integration_item_code": medusa_product_id,
			"has_variants": 1,
		},
		["name", "erpnext_item_code"],
		as_dict=True,
	)
	if template and template.erpnext_item_code:
		extra = frappe.get_all(
			"Ecommerce Item",
			filters={
				"integration": MODULE_NAME,
				"variant_of": template.erpnext_item_code,
				"has_variants": 0,
			},
			fields=[
				"name",
				"erpnext_item_code",
				"integration_item_code",
				"variant_id",
				"variant_of",
				"has_variants",
				"sku",
			],
		)
		seen = {r.name for r in rows}
		for r in extra:
			if r.name not in seen:
				rows.append(r)
				seen.add(r.name)
	return rows


def mark_orphaned(medusa_product_id: str, variant_id: str | None = None) -> None:
	"""No-op for schema: Ecommerce Item has no status field.

	Callers still disable the ERPNext Item; the Ecommerce Item row is kept for
	identity history (Shopify behaviour).
	"""
	return


def get_mapping_health() -> dict:
	"""Detect duplicates and missing ERPNext items for Medusa Ecommerce Items."""
	all_maps = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME},
		fields=[
			"name",
			"erpnext_item_code",
			"integration_item_code",
			"variant_id",
			"sku",
			"has_variants",
		],
	)
	missing_items: list[dict] = []
	seen_keys: dict[tuple, list[str]] = {}
	duplicates: list[dict] = []
	legacy_product_keyed: list[dict] = []

	for row in all_maps:
		vid = cstr(row.variant_id)
		key = (row.integration_item_code, vid)
		seen_keys.setdefault(key, []).append(row.name)
		if not frappe.db.exists("Item", row.erpnext_item_code):
			missing_items.append(row)
		# Sellable still keyed by product id (should be migrated)
		if (
			not cint_safe(row.has_variants)
			and cstr(row.integration_item_code).startswith("prod_")
			and vid
			and cstr(row.integration_item_code) != vid
		):
			legacy_product_keyed.append(row)

	for key, names in seen_keys.items():
		if len(names) > 1:
			duplicates.append(
				{
					"integration_item_code": key[0],
					"medusa_variant_id": key[1],
					"names": names,
				}
			)

	return {
		"total": len(all_maps),
		"orphaned_count": 0,
		"missing_item_count": len(missing_items),
		"duplicate_count": len(duplicates),
		"legacy_product_keyed_count": len(legacy_product_keyed),
		"orphaned": [],
		"missing_items": missing_items[:50],
		"duplicates": duplicates[:50],
		"legacy_product_keyed": legacy_product_keyed[:50],
	}


def get_ecommerce_item_name(
	medusa_product_id: str | None = None,
	variant_id: str | None = None,
	has_variants: int = 0,
) -> str | None:
	"""Return Ecommerce Item name for a Medusa product/variant."""
	variant_id = cstr(variant_id) if variant_id else None
	if has_variants and medusa_product_id:
		return frappe.db.get_value(
			"Ecommerce Item",
			{
				"integration": MODULE_NAME,
				"integration_item_code": medusa_product_id,
				"has_variants": 1,
			},
			"name",
		)
	if variant_id:
		name = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "integration_item_code": variant_id},
			"name",
		)
		if name:
			return name
		return frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "variant_id": variant_id},
			"name",
		)
	if medusa_product_id:
		return frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "integration_item_code": medusa_product_id},
			"name",
		)
	return None


def cint_safe(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0
