# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from medusa_connector.constants import MODULE_NAME, MedusaOperationStatus


def result(status: str | MedusaOperationStatus, **kwargs) -> dict[str, Any]:
	# Handle both string and Enum values for backward compatibility
	status_value = status.value if isinstance(status, MedusaOperationStatus) else status
	out = {"status": status_value}
	out.update({key: value for key, value in kwargs.items() if value is not None})
	return out


def status_label(order: dict) -> str:
	parts = [
		cstr(order.get("status")),
		cstr(order.get("payment_status")),
		cstr(order.get("fulfillment_status")),
	]
	return " / ".join(part for part in parts if part)[:140]


def cancel_doc(doctype: str, name: str) -> bool:
	doc = frappe.get_doc(doctype, name)

	if doc.docstatus == 2:
		return True

	if doc.docstatus != 1:
		return False

	doc.cancel()
	return True


def resolve_item_code(line_item: dict) -> str | None:
	from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import get_erpnext_item

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
