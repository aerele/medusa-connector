# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Sales Invoice from Medusa paid orders (Shopify-style)."""

from __future__ import annotations

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from medusa_connector.constants import ORDER_ID_FIELD, ORDER_NUMBER_FIELD, ORDER_STATUS_FIELD


def create_sales_invoice(order: dict, settings, sales_order) -> str | None:
	"""Create and submit SI (+ optional PE) when settings allow and order is unpaid in ERPNext."""
	order_id = cstr(order.get("id") or "")
	if not order_id or not sales_order:
		return None

	if frappe.db.exists("Sales Invoice", {ORDER_ID_FIELD: order_id}):
		return frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id}, "name")

	if sales_order.docstatus != 1 or flt_safe(sales_order.per_billed) >= 100:
		return None

	if not cint(settings.get("sync_sales_invoice")):
		return None

	posting_date = getdate(order.get("created_at")) or nowdate()
	si = make_sales_invoice(sales_order.name, ignore_permissions=True)
	si.set(ORDER_ID_FIELD, order_id)
	si.set(
		ORDER_NUMBER_FIELD,
		cstr(order.get("display_id") or order.get("custom_display_id") or order_id),
	)
	si.set(ORDER_STATUS_FIELD, cstr(order.get("payment_status") or order.get("status") or ""))
	si.set_posting_time = 1
	si.posting_date = posting_date
	si.due_date = posting_date
	si.naming_series = settings.get("sales_invoice_series") or "SI-MED-"
	si.flags.ignore_mandatory = True

	if settings.get("cost_center"):
		for row in si.items:
			row.cost_center = settings.cost_center

	si.insert(ignore_permissions=True, ignore_mandatory=True)
	si.submit()

	if si.grand_total > 0 and settings.get("cash_bank_account"):
		_make_payment_entry(si, settings, posting_date)

	return si.name


def _make_payment_entry(sales_invoice, settings, posting_date) -> None:
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry(
		sales_invoice.doctype,
		sales_invoice.name,
		bank_account=settings.cash_bank_account,
	)
	pe.flags.ignore_mandatory = True
	pe.reference_no = sales_invoice.name
	pe.posting_date = posting_date or nowdate()
	pe.reference_date = posting_date or nowdate()
	pe.insert(ignore_permissions=True)
	pe.submit()


def flt_safe(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0
