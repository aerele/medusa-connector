# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Shared helpers for the order domain.

Kept deliberately small — only logic genuinely used by more than one
order-domain service lives here (``OrderSync``, ``SalesOrderSync``,
``FulfillmentSync``, ``RefundSync``). Payment-specific helpers live in
``payment_sync.py`` instead.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from medusa_connector.constants import MODULE_NAME


def result(status: str, **kwargs) -> dict[str, Any]:
	out: dict[str, Any] = {"status": status}
	out.update({k: v for k, v in kwargs.items() if v is not None})
	return out


def status_label(order: dict) -> str:
	parts = [
		cstr(order.get("status") or ""),
		cstr(order.get("payment_status") or ""),
		cstr(order.get("fulfillment_status") or ""),
	]
	return " / ".join(p for p in parts if p)[:140]


def cancel_doc(doctype: str, name: str) -> bool:
	"""Cancel a submitted document. True if cancelled (or already cancelled)."""
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus == 2:
		return True
	if doc.docstatus != 1:
		return False
	doc.cancel()
	return True


def resolve_item_code(line_item: dict) -> str | None:
	from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import (
		get_erpnext_item,
	)

	if not isinstance(line_item, dict):
		return None

	variant = line_item.get("variant")
	if not isinstance(variant, dict):
		variant = {}

	product_id = line_item.get("product_id")
	if not product_id:
		return None

	variant_id = line_item.get("variant_id") or variant.get("id")
	sku = line_item.get("variant_sku") or variant.get("sku") or line_item.get("sku")

	item = get_erpnext_item(
		MODULE_NAME,
		cstr(product_id),
		variant_id=cstr(variant_id) if variant_id else None,
		sku=cstr(sku) if sku else None,
	)

	return item.name if item else None
