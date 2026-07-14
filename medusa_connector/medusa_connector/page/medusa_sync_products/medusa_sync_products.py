# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Whitelisted API for the Sync Medusa Products desk page."""

from __future__ import annotations

import frappe

from medusa_connector.product.import_products import (
	get_product_counts,
	health_check,
	import_single_product,
	is_product_sync_running,
	list_medusa_products,
	retry_failed_products,
	start_product_sync,
)
from medusa_connector.product.item_mapping import (
	get_mapping_health,
)


@frappe.whitelist()
def get_product_count() -> dict:
	return get_product_counts()


@frappe.whitelist()
def get_products(offset: int = 0, limit: int = 20, q: str | None = None, status: str | None = None) -> dict:
	return list_medusa_products(offset=offset, limit=limit, q=q, status=status)


@frappe.whitelist()
def sync_product(product_id: str) -> dict:
	return import_single_product(product_id, force=True)


@frappe.whitelist()
def resync_product(product_id: str) -> dict:
	return import_single_product(product_id, force=True)


@frappe.whitelist()
def start_sync(mode: str = "Full", q: str | None = None, status: str | None = None, force: int = 1) -> dict:
	return start_product_sync(mode=mode, q=q, status=status, force=force)


@frappe.whitelist()
def sync_status() -> dict:
	return {"running": is_product_sync_running()}


@frappe.whitelist()
def get_health() -> dict:
	return health_check()


@frappe.whitelist()
def mapping_health() -> dict:
	return get_mapping_health()


@frappe.whitelist()
def retry_failed(log_name: str) -> dict:
	return retry_failed_products(log_name)
