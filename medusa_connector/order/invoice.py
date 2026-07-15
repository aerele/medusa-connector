# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Sales Invoice from Medusa paid orders (standard)."""

from __future__ import annotations

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from medusa_connector.constants import ORDER_ID_FIELD, ORDER_NUMBER_FIELD, ORDER_STATUS_FIELD


def create_sales_invoice(
	order: dict,
	settings,
	sales_order,
	*,
	payment_id: str | None = None,
) -> str | None:
	"""Create and submit SI (+ optional PE) when settings allow and order is unpaid in ERPNext.

	Idempotent on ``medusa_order_id`` for SI. Payment Entry uses Medusa ``payment_id``
	as ``reference_no`` when provided so duplicate ``payment.captured`` events do not
	create a second PE.
	"""
	order_id = cstr(order.get("id") or "")
	if not order_id or not sales_order:
		return None

	if not cint(settings.get("sync_sales_invoice")):
		return None

	existing_si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id}, "name")
	if existing_si:
		# SI already exists — still try PE if outstanding and payment not yet recorded.
		si = frappe.get_doc("Sales Invoice", existing_si)
		posting_date = getdate(order.get("created_at")) or nowdate()
		_ensure_payment_entry(si, settings, posting_date, payment_id=payment_id, order_id=order_id)
		return existing_si

	if sales_order.docstatus != 1 or flt_safe(sales_order.per_billed) >= 100:
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

	_ensure_payment_entry(si, settings, posting_date, payment_id=payment_id, order_id=order_id)

	return si.name


def _ensure_payment_entry(
	sales_invoice,
	settings,
	posting_date,
	*,
	payment_id: str | None = None,
	order_id: str | None = None,
) -> str | None:
	"""Create Payment Entry against SI if needed; skip when already paid or PE exists."""
	if not settings.get("cash_bank_account"):
		return None
	if flt_safe(sales_invoice.grand_total) <= 0:
		return None

	# Reload outstanding after possible prior PE
	outstanding = flt_safe(frappe.db.get_value("Sales Invoice", sales_invoice.name, "outstanding_amount"))
	if outstanding <= 0:
		return None

	reference_no = cstr(payment_id or "") or sales_invoice.name

	# Idempotency: same Medusa payment id (or SI name) already used on a submitted PE
	existing_pe = frappe.db.get_value(
		"Payment Entry",
		{"reference_no": reference_no, "docstatus": 1},
		"name",
	)
	if existing_pe:
		return existing_pe

	return _make_payment_entry(
		sales_invoice, settings, posting_date, reference_no=reference_no, order_id=order_id
	)


def _make_payment_entry(
	sales_invoice, settings, posting_date, *, reference_no: str | None = None, order_id: str | None = None
) -> str | None:
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry(
		sales_invoice.doctype,
		sales_invoice.name,
		bank_account=settings.cash_bank_account,
	)
	pe.flags.ignore_mandatory = True
	pe.reference_no = cstr(reference_no or sales_invoice.name)
	pe.posting_date = posting_date or nowdate()
	pe.reference_date = posting_date or nowdate()
	# Stamp Medusa order id so the refund workflow can locate this PE by order.
	if order_id and frappe.get_meta("Payment Entry").has_field(ORDER_ID_FIELD):
		pe.set(ORDER_ID_FIELD, order_id)
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name


def flt_safe(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0
