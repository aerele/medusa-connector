# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""ERPNext → Medusa inventory levels (scheduled one-way push)."""

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
from frappe.utils import cint, create_batch, now

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.medusa.product import ProductService
from medusa_connector.utils.logging import create_sync_log

BATCH_SIZE = 50
SYNC_LOG_METHOD = "medusa_connector.product.inventory_export.update_inventory_on_medusa"


def update_inventory_on_medusa() -> None:
	settings = frappe.get_cached_doc(SETTING_DOCTYPE)

	if not settings.enabled:
		return
	if not settings.get("update_erpnext_stock_levels_to_medusa"):
		return
	if not need_to_run(
		SETTING_DOCTYPE,
		"inventory_sync_frequency",
		"last_inventory_sync",
	):
		return

	_sync_inventory(settings)


@frappe.whitelist()
def sync_inventory_now() -> dict:
	settings = frappe.get_cached_doc(SETTING_DOCTYPE)

	if not settings.enabled:
		frappe.throw(
			_("Please enable the Medusa Connector first."),
			title=_("Medusa Connector"),
		)
	if not settings.get("update_erpnext_stock_levels_to_medusa"):
		frappe.throw(
			_("Please enable Update Stock Levels to Medusa first."),
			title=_("Inventory Sync"),
		)

	return _sync_inventory(settings)


def _sync_inventory(settings) -> dict:
	warehouse_map = settings.get_erpnext_to_medusa_wh_mapping()

	if not warehouse_map:
		return {
			"status": "Success",
			"message": _("No warehouse mappings configured."),
			"updated": 0,
			"failed": 0,
		}

	inventory_levels = _get_inventory_levels(warehouse_map)
	if not inventory_levels:
		return {
			"status": "Success",
			"message": _("No inventory changes to push."),
			"updated": 0,
			"failed": 0,
		}

	return upload_inventory_data_to_medusa(inventory_levels, warehouse_map)


def upload_inventory_data_to_medusa(
	inventory_levels,
	warehouse_map: dict[str, str],
) -> dict:
	"""Push ERPNext stock levels to mapped Medusa locations."""
	inventory_service = InventoryService()
	product_service = ProductService()
	product_title_cache: dict[str, str | None] = {}
	synced_on = now()
	results = []

	for batch in create_batch(inventory_levels, BATCH_SIZE):
		for d in batch:
			_apply_inventory_update(
				d,
				warehouse_map,
				product_service,
				inventory_service,
				synced_on,
				product_title_cache,
			)
			results.append(d)
		frappe.db.commit()
		_log_inventory_update_status(batch)

	return _get_sync_result(results)


def _apply_inventory_update(
	d,
	warehouse_map,
	product_service,
	inventory_service,
	synced_on,
	product_title_cache: dict[str, str | None],
) -> None:
	"""Synchronize one ERPNext inventory row to Medusa."""
	d.medusa_location_id = warehouse_map.get(d.warehouse)
	d.status = "Failed"
	d.failure_reason = ""
	d.qty = None

	if not d.medusa_location_id:
		d.failure_reason = f"No Medusa location mapped for warehouse {d.warehouse}"
		return

	if not d.variant_id:
		d.status = "Not Found"
		d.failure_reason = "No Medusa variant ID found"
		return

	try:
		# Get Medusa variant with inventory item relations.
		variant = (
			product_service.get_variant(
				d.variant_id,
				fields="*inventory_items,+sku,+title,+metadata",
			)
			or {}
		)
		if not variant:
			d.status = "Not Found"
			d.failure_reason = f"Medusa variant {d.variant_id} was not found."
			return

		# Get product title for inventory item naming — cached per batch run
		# so multiple variants of the same product don't each re-fetch it.
		product_title = None
		product_id = variant.get("product_id")
		if product_id:
			if product_id not in product_title_cache:
				product = (
					product_service.get_product(
						product_id,
						fields="*",
					)
					or {}
				)
				product_title_cache[product_id] = product.get("title")
			product_title = product_title_cache[product_id]

		inventory_items = _get_inventory_items(variant)
		inventory_item_count = len(inventory_items)

		# Multiple inventory items exist; fail safely without guessing which one should receive ERPNext stock.
		if inventory_item_count > 1:
			inventory_item_ids = [
				inventory_item_id
				for item in inventory_items
				if (inventory_item_id := _get_inventory_item_id_from_relation(item))
			]
			d.status = "Failed"
			d.failure_reason = (
				f"Multiple inventory items "
				f"({inventory_item_count}) are linked to "
				f"Medusa variant {d.variant_id}. "
				f"Expected exactly one inventory item. "
				f"Inventory item IDs: "
				f"{', '.join(inventory_item_ids) or 'unknown'}."
			)
			return

		# No inventory item; create one directly with variant_id so Medusa creates the relation.
		if inventory_item_count == 0:
			inventory_item_id = inventory_service.ensure_inventory_item_for_variant(
				d.variant_id,
				variant=variant,
				product_title=product_title,
			)

			# Re-fetch the variant after creation to verify that the new inventory item is linked before updating stock.
			updated_variant = (
				product_service.get_variant(
					d.variant_id,
					fields="*inventory_items",
				)
				or {}
			)
			updated_inventory_items = _get_inventory_items(updated_variant)

			if len(updated_inventory_items) != 1:
				d.status = "Failed"
				d.failure_reason = (
					f"Inventory item {inventory_item_id} was created "
					f"for Medusa variant {d.variant_id}, but the "
					f"variant does not have exactly one linked "
					f"inventory item after creation. "
					f"Found {len(updated_inventory_items)}."
				)
				return

			verified_inventory_item_id = _get_inventory_item_id_from_relation(updated_inventory_items[0])
			if not verified_inventory_item_id or verified_inventory_item_id != inventory_item_id:
				d.status = "Failed"
				d.failure_reason = (
					f"Inventory item {inventory_item_id} was created "
					f"for Medusa variant {d.variant_id}, but the "
					f"created inventory item could not be verified "
					f"as the variant's linked inventory item."
				)
				return

			inventory_item_id = verified_inventory_item_id

		# Exactly one inventory item exists; reuse it and never create another one.
		else:
			inventory_item_id = _get_inventory_item_id_from_relation(inventory_items[0])
			if not inventory_item_id:
				d.status = "Failed"
				d.failure_reason = (
					f"Exactly one inventory item relation exists "
					f"for Medusa variant {d.variant_id}, but its "
					f"inventory item ID could not be resolved."
				)
				return

		available_qty = max(cint(d.actual_qty) - cint(d.reserved_qty), 0)
		d.qty = available_qty

		inventory_service.set_stocked_quantity(
			inventory_item_id,
			d.medusa_location_id,
			available_qty,
		)

		# Mark Ecommerce Item inventory sync timestamp.
		update_inventory_sync_status(d.ecom_item, time=synced_on)
		d.status = "Success"

	except Exception as exc:
		d.status = "Failed"
		d.failure_reason = str(exc)


def _get_inventory_levels(warehouse_map: dict[str, str]) -> list:
	warehouses = [w for w in warehouse_map if w]
	if not warehouses:
		return []

	group_flags = {
		row.name: cint(row.is_group)
		for row in frappe.get_all(
			"Warehouse",
			filters={"name": ["in", warehouses]},
			fields=["name", "is_group"],
		)
	}

	levels = []
	for warehouse in warehouses:
		is_group = group_flags.get(warehouse, 0)
		rows = (
			get_inventory_levels_of_group_warehouse(warehouse, MODULE_NAME)
			if is_group
			else get_inventory_levels((warehouse,), MODULE_NAME)
		)
		levels.extend(row for row in rows if row.get("variant_id"))

	return levels


def _get_inventory_items(variant: dict) -> list[dict]:
	"""Return all inventory item relations from a Medusa variant."""
	items = variant.get("inventory_items")
	if not items:
		return []
	if isinstance(items, dict):
		items = [items]
	return [item for item in items if isinstance(item, dict)]


def _get_inventory_item_id_from_relation(item: dict) -> str | None:
	"""Extract an inventory item ID from a variant relation."""
	if not isinstance(item, dict):
		return None

	if item.get("inventory_item_id"):
		return item["inventory_item_id"]

	inventory_item = item.get("inventory_item")
	if isinstance(inventory_item, dict) and inventory_item.get("id"):
		return inventory_item["id"]

	if item.get("id"):
		return item["id"]

	return None


def _status_label(success: int, total: int) -> str:
	"""Return overall sync status."""
	if total and success == total:
		return "Success"
	return "Partial Success" if success else "Failed"


def _log_inventory_update_status(batch) -> None:
	"""Create a sync log for one inventory batch."""
	if not batch:
		return

	stats = Counter(d.status for d in batch)
	total = len(batch)
	success = stats.get("Success", 0)
	percent = success / total if total else 0

	rows = "\n".join(
		(f"{d.variant_id},{d.medusa_location_id},{getattr(d, 'qty', '')},{d.status},{d.failure_reason or ''}")
		for d in batch
	)

	message = (
		f"Updated {percent * 100:.0f}% items\n\nvariant_id,location_id,qty,status,failure_reason\n{rows}"
	)

	create_sync_log(
		sync_type="Inventory Export",
		status=_status_label(success, total),
		method=SYNC_LOG_METHOD,
		message=message,
	)


def _get_sync_result(results) -> dict:
	"""Return inventory sync result summary."""
	stats = Counter(d.status for d in results)
	total = len(results)
	success = stats.get("Success", 0)
	not_found = stats.get("Not Found", 0)
	failed = stats.get("Failed", 0)

	return {
		"status": _status_label(success, total),
		"message": _("Updated {0} items: {1} successful, {2} not found, {3} failed.").format(
			total, success, not_found, failed
		),
		"updated": success,
		"failed": failed + not_found,
		"total": total,
	}
