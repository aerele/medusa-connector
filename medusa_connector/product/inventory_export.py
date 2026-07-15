# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""ERPNext → Medusa inventory levels (scheduled one-way push).

Uses Ecommerce Core inventory helpers + Ecommerce Item + Ecommerce Integration Log.
"""

from __future__ import annotations

from collections import Counter

import frappe
from ecommerce_core.controllers.inventory import (
	get_inventory_levels,
	get_inventory_levels_of_group_warehouse,
	update_inventory_sync_status,
)
from ecommerce_core.controllers.scheduling import need_to_run
from frappe import _
from frappe.utils import cint, create_batch, cstr, now

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.medusa.product import ProductService
from medusa_connector.utils.logging import create_sync_log, update_sync_log

SYNC_LOCK_KEY = "medusa_connector:inventory_sync_lock"
SYNC_LOCK_TTL = 900
BATCH_SIZE = 50
MAX_ROWS = 1000


def update_inventory_on_medusa(force: bool = False) -> dict | None:
	"""Upload stock levels from ERPNext to Medusa."""
	settings = frappe.get_doc(SETTING_DOCTYPE)

	if not settings.enabled or not settings.get("update_erpnext_stock_levels_to_medusa"):
		return None

	wh_map = settings.get_erpnext_to_medusa_wh_mapping()
	if not wh_map:
		return None

	if not force and not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return None

	cache = frappe.cache()
	if not cache.set(SYNC_LOCK_KEY, "1", nx=True, ex=SYNC_LOCK_TTL):
		return {
			"status": "Busy",
			"message": frappe._("An inventory sync is already running."),
			"updated": 0,
			"failed": 0,
		}

	try:
		if force:
			frappe.db.set_value(SETTING_DOCTYPE, None, "last_inventory_sync", now(), update_modified=False)

		levels = _collect_inventory_levels(wh_map)
		if not levels:
			return {
				"status": "Success",
				"message": frappe._("No inventory changes to push."),
				"updated": 0,
				"failed": 0,
			}

		return upload_inventory_levels(levels, wh_map)
	finally:
		cache.delete(SYNC_LOCK_KEY)


def upload_inventory_levels(levels: list[dict], warehouse_map: dict[str, str]) -> dict:
	inventory = InventoryService()
	products = ProductService()
	# Cache inventory_item_id by (product_id, variant_id) within one push run.
	inventory_item_cache: dict[tuple[str | None, str | None], str | None] = {}
	synced_on = now()
	results: list[frappe._dict] = []

	log_name = create_sync_log(
		sync_type="Inventory Export",
		status="Running",
		method="medusa_connector.product.inventory_export.update_inventory_on_medusa",
		message=frappe._("Pushing {0} inventory rows across {1} warehouse(s)").format(
			len(levels), len(warehouse_map)
		),
	)

	for batch in create_batch(levels, BATCH_SIZE):
		for row in batch:
			entry = frappe._dict(row)
			entry.status = "Failed"
			entry.failure_reason = ""
			entry.qty = max(cint(entry.actual_qty) - cint(entry.reserved_qty), 0)
			location_id = entry.get("medusa_location_id") or warehouse_map.get(entry.warehouse)
			entry.medusa_location_id = location_id or ""
			ecom_name = entry.get("ecom_item") or entry.get("mapping_name")

			try:
				if not location_id:
					entry.status = "Failed"
					entry.failure_reason = f"No Medusa location mapped for warehouse {entry.warehouse}"
				else:
					# integration_item_code is Medusa Variant ID for sellable rows.
					variant_id = (
						entry.get("variant_id")
						or entry.get("medusa_variant_id")
						or entry.get("integration_item_code")
					)
					product_id = entry.get("medusa_product_id")
					# Ignore legacy product-id still sitting in integration_item_code.
					if product_id and cstr(product_id).startswith("variant_"):
						product_id = None
					code = cstr(entry.get("integration_item_code") or "")
					if code.startswith("prod_"):
						product_id = product_id or code
						if not entry.get("variant_id"):
							variant_id = None
					cache_key = (product_id or None, variant_id or None)
					if cache_key not in inventory_item_cache:
						inventory_item_cache[cache_key] = _resolve_inventory_item_id(
							products, product_id, variant_id
						)
					inventory_item_id = inventory_item_cache[cache_key]
					if not inventory_item_id:
						# Do not touch inventory_synced_on — leave row eligible for retry.
						entry.status = "Not Found"
						entry.failure_reason = "No inventory item on Medusa variant"
					else:
						inventory.set_stocked_quantity(inventory_item_id, location_id, entry.qty)
						if ecom_name:
							update_inventory_sync_status(ecom_name, time=synced_on)
						entry.status = "Success"
						entry.medusa_variant_id = variant_id
						entry.inventory_item_id = inventory_item_id
			except Exception as exc:
				entry.status = "Failed"
				entry.failure_reason = str(exc)

			results.append(entry)
			frappe.db.commit()

	return _finish_log(log_name, results)


def _collect_inventory_levels(warehouse_map: dict[str, str]) -> list[dict]:
	levels: list[dict] = []
	for warehouse, location_id in warehouse_map.items():
		if not warehouse or not location_id:
			continue
		is_group = cint(frappe.db.get_value("Warehouse", warehouse, "is_group"))
		if is_group:
			rows = get_inventory_levels_of_group_warehouse(warehouse, MODULE_NAME)
		else:
			rows = get_inventory_levels((warehouse,), MODULE_NAME)
		for row in rows:
			row["medusa_location_id"] = location_id
			row["warehouse"] = row.get("warehouse") or warehouse
			# Sellable: integration_item_code = variant id; product id is not stored there.
			code = cstr(row.get("integration_item_code") or "")
			vid = cstr(row.get("variant_id") or "") or (code if code.startswith("variant_") else "")
			row["medusa_variant_id"] = vid
			row["variant_id"] = vid
			row["medusa_product_id"] = code if code.startswith("prod_") else ""
			row["mapping_name"] = row.get("ecom_item")
			# Skip template Ecommerce Items (has_variants=1) — no stock on templates.
			if not vid and code.startswith("prod_"):
				continue
			levels.append(row)
		if len(levels) >= MAX_ROWS:
			break
	return levels[:MAX_ROWS]


# Product GET must expand inventory_items; default product fields omit them.
_INVENTORY_PRODUCT_FIELDS = "*variants,*variants.inventory_items"
_INVENTORY_VARIANT_FIELDS = "*inventory_items"


def _inventory_item_id_from_links(links) -> str | None:
	for link in links or []:
		if not isinstance(link, dict):
			continue
		if link.get("inventory_item_id"):
			return link["inventory_item_id"]
		inv = link.get("inventory_item") or {}
		if isinstance(inv, dict) and inv.get("id"):
			return inv["id"]
	return None


def _resolve_inventory_item_id(
	products: ProductService, product_id: str | None, variant_id: str | None
) -> str | None:
	"""Resolve Medusa inventory_item_id for a product variant.

	Primary path is Variant ID (``integration_item_code`` on sellable Ecommerce
	Items). Product ID is optional (batch expand when known).
	"""
	# 1) Admin product-variants with inventory_items expand (preferred).
	if variant_id:
		variant = products.get_variant(variant_id, fields=_INVENTORY_VARIANT_FIELDS) or {}
		found = _inventory_item_id_from_links(variant.get("inventory_items"))
		if found:
			return found
		# If product_id unknown, recover from the variant payload.
		if not product_id:
			product_id = variant.get("product_id") or (
				(variant.get("product") or {}).get("id") if isinstance(variant.get("product"), dict) else None
			)

	# 2) Product payload with inventory_items expand (multi-variant batch).
	if product_id:
		product = products.get_product(product_id, fields=_INVENTORY_PRODUCT_FIELDS) or {}
		for variant in product.get("variants") or []:
			if variant_id and variant.get("id") != variant_id:
				continue
			found = _inventory_item_id_from_links(variant.get("inventory_items"))
			if found:
				return found
			if not variant_id:
				break

	return None


def _finish_log(log_name: str, results: list) -> dict:
	stats = Counter(r.status for r in results)
	ok = stats.get("Success", 0)
	not_found = stats.get("Not Found", 0)
	failed = stats.get("Failed", 0)
	total = len(results) or 1
	ratio = ok / total

	if ok and (failed or not_found):
		status = "Partial Success"
	elif not ok and not_found and not failed:
		status = "Partial Success"
	elif not ok and failed:
		status = "Failed"
	elif ok and not failed and not not_found:
		status = "Success"
	else:
		status = "Partial Success"

	# Ecommerce Integration Log uses free-text status (Success/Error/…).
	log_status = "Success" if status == "Success" else "Error"
	if status == "Partial Success":
		log_status = "Error"

	message_lines = [
		f"Updated {ratio * 100:.0f}% items ({ok} success, {not_found} not found, {failed} failed)",
		"variant_id,warehouse,location_id,qty,status,failure_reason",
	]
	for r in results:
		message_lines.append(
			f"{getattr(r, 'medusa_variant_id', '') or getattr(r, 'variant_id', '')},"
			f"{r.warehouse},{getattr(r, 'medusa_location_id', '')},"
			f"{getattr(r, 'qty', '')},{r.status},{r.failure_reason or ''}"
		)

	update_sync_log(
		log_name,
		status=log_status,
		message="\n".join(message_lines),
		updated=ok,
		failed=failed + not_found,
		complete=True,
	)
	frappe.db.commit()

	return {
		"status": status,
		"message": message_lines[0],
		"updated": ok,
		"failed": failed + not_found,
		"total": len(results),
	}


@frappe.whitelist()
def sync_inventory_now() -> dict:
	"""Force one inventory push (ignores frequency gate)."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled:
		frappe.throw(_("Please enable the Medusa Connector first."), title=_("Medusa Connector"))
	if not settings.get("update_erpnext_stock_levels_to_medusa"):
		frappe.throw(
			_("Please enable Update Stock Levels to Medusa first."),
			title=_("Inventory Sync"),
		)
	if not settings.get_erpnext_to_medusa_wh_mapping():
		frappe.throw(
			_("Please add at least one enabled Warehouse Mapping before syncing inventory."),
			title=_("Inventory Sync"),
		)

	result = update_inventory_on_medusa(force=True)
	if result is None:
		return {
			"status": "Skipped",
			"message": _("Inventory sync did not run. Check that it is enabled and configured."),
		}
	return result
