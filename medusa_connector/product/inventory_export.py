# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""ERPNext → Medusa inventory levels (Shopify-style scheduled push).

Uses Medusa Admin inventory endpoints:
- Resolve inventory item from product variant (``variants.inventory_items``)
- ``POST /admin/inventory-items/{id}/location-levels/{location_id}`` with stocked_quantity
"""

from __future__ import annotations

from collections import Counter

import frappe
from frappe.utils import cint, now, now_datetime

from medusa_connector.constants import SETTING_DOCTYPE
from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.medusa.product import ProductService
from medusa_connector.medusa_connector.doctype.medusa_sync_log.medusa_sync_log import (
	create_sync_log,
	update_sync_log,
)


def update_inventory_on_medusa() -> None:
	"""Scheduler entry: push Bin quantities to Medusa location levels."""
	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.get("update_erpnext_stock_levels_to_medusa"):
		return
	if not settings.get("default_location_id"):
		return
	if not settings.get("warehouse"):
		return
	if not _need_to_run(settings):
		return

	levels = _get_inventory_levels(settings.warehouse)
	if not levels:
		frappe.db.set_value(
			SETTING_DOCTYPE,
			SETTING_DOCTYPE,
			"last_inventory_sync",
			now_datetime(),
			update_modified=False,
		)
		return

	upload_inventory_levels(levels, settings)


def upload_inventory_levels(levels: list[dict], settings) -> None:
	inventory = InventoryService()
	products = ProductService()
	location_id = settings.default_location_id
	synced_on = now()
	results = []

	log_name = create_sync_log(
		sync_type="Inventory Export",
		status="Running",
		method="medusa_connector.product.inventory_export.update_inventory_on_medusa",
		message=f"Pushing {len(levels)} inventory rows",
	)

	for row in levels:
		entry = frappe._dict(row)
		entry.status = "Failed"
		entry.failure_reason = ""
		try:
			inventory_item_id = entry.medusa_inventory_item_id or _resolve_inventory_item_id(
				products, entry.medusa_product_id, entry.medusa_variant_id
			)
			if not inventory_item_id:
				entry.status = "Not Found"
				entry.failure_reason = "No inventory item on Medusa variant"
			else:
				qty = max(cint(entry.actual_qty) - cint(entry.reserved_qty), 0)
				inventory.set_stocked_quantity(inventory_item_id, location_id, qty)
				frappe.db.set_value(
					"Medusa Item Mapping",
					entry.mapping_name,
					{
						"inventory_synced_on": synced_on,
						"medusa_inventory_item_id": inventory_item_id,
					},
					update_modified=False,
				)
				entry.status = "Success"
		except Exception as exc:
			entry.status = "Failed"
			entry.failure_reason = str(exc)
		results.append(entry)
		frappe.db.commit()

	stats = Counter(r.status for r in results)
	ok = stats.get("Success", 0)
	total = len(results) or 1
	ratio = ok / total
	if ratio == 0:
		status = "Failed"
	elif ratio < 1:
		status = "Partial Success"
	else:
		status = "Success"

	message_lines = [
		f"Updated {ratio * 100:.0f}% items",
		"variant_id,location_id,status,failure_reason",
	]
	for r in results:
		message_lines.append(f"{r.medusa_variant_id},{location_id},{r.status},{r.failure_reason or ''}")
	update_sync_log(
		log_name,
		status=status,
		message="\n".join(message_lines),
		updated=ok,
		failed=stats.get("Failed", 0) + stats.get("Not Found", 0),
		complete=True,
	)
	frappe.db.set_value(
		SETTING_DOCTYPE,
		SETTING_DOCTYPE,
		"last_inventory_sync",
		now_datetime(),
		update_modified=False,
	)
	frappe.db.commit()


def _resolve_inventory_item_id(
	products: ProductService, product_id: str | None, variant_id: str | None
) -> str | None:
	if not product_id:
		return None
	product = products.get_product(product_id)
	for variant in product.get("variants") or []:
		if variant_id and variant.get("id") != variant_id:
			continue
		for link in variant.get("inventory_items") or []:
			# Relation shapes: {inventory_item_id} or nested inventory_item
			if link.get("inventory_item_id"):
				return link["inventory_item_id"]
			inv = link.get("inventory_item") or {}
			if inv.get("id"):
				return inv["id"]
		if not variant_id:
			break
	return None


def _get_inventory_levels(warehouse: str) -> list[dict]:
	"""Items whose Bin changed after last inventory sync stamp on the mapping."""
	return frappe.db.sql(
		"""
		select
			m.name as mapping_name,
			m.erpnext_item_code as item_code,
			m.medusa_product_id,
			m.medusa_variant_id,
			m.medusa_inventory_item_id,
			b.actual_qty,
			b.reserved_qty,
			b.warehouse
		from `tabMedusa Item Mapping` m
		inner join `tabBin` b on b.item_code = m.erpnext_item_code
		where b.warehouse = %s
			and ifnull(m.has_variants, 0) = 0
			and ifnull(m.medusa_variant_id, '') != ''
			and m.status = 'Active'
			and (
				m.inventory_synced_on is null
				or b.modified > m.inventory_synced_on
			)
		""",
		(warehouse,),
		as_dict=True,
	)


def _need_to_run(settings) -> bool:
	"""Simple frequency gate (minutes) using last_inventory_sync."""
	freq = cint(settings.get("inventory_sync_frequency") or 15)
	last = settings.get("last_inventory_sync")
	if not last:
		return True
	from frappe.utils import time_diff_in_seconds

	return time_diff_in_seconds(now_datetime(), last) >= freq * 60
