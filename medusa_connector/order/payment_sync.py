# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Payment-state helpers + Payment Entry reconciliation for one Medusa order.

Split out from ``InvoiceSync`` on purpose: the Sales Invoice represents the
*billing* event, the Payment Entry represents the *receipt* event, and
Medusa can send either independently (e.g. ``payment.captured`` arriving
before the order has been invoiced). Keeping them separate means either
service can run on its own without the other reaching into it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt, nowdate

from medusa_connector.constants import ORDER_ID_FIELD


class PaymentSync:
	def __init__(self, settings=None):
		self.settings = settings

	# -- status helpers ---------------------------------------------------
	@staticmethod
	def is_paid(order: dict) -> bool:
		return cstr(order.get("payment_status") or "").lower() in {"captured", "paid"}

	@staticmethod
	def is_refunded(order: dict) -> bool:
		return cstr(order.get("payment_status") or "").lower() in {"refunded", "partially_refunded"}

	@staticmethod
	def first_payment_id(order: dict) -> str | None:
		for collection in order.get("payment_collections") or []:
			for payment in collection.get("payments") or []:
				pid = cstr(payment.get("id"))
				if pid:
					return pid
		return None

	@staticmethod
	def captured_payment_id(order: dict) -> str | None:
		for collection in order.get("payment_collections") or []:
			for payment in collection.get("payments") or []:
				if payment.get("captured_at") or cstr(payment.get("status") or "").lower() == "captured":
					pid = cstr(payment.get("id"))
					if pid:
						return pid
		return None

	# -- Payment Entry ------------------------------------------------------
	def reconcile(
		self,
		sales_invoice,
		posting_date=None,
		*,
		payment_id: str | None = None,
		order_id: str | None = None,
	) -> str | None:
		"""Ensure a Payment Entry exists for an already-submitted, outstanding
		Sales Invoice. Idempotent on ``reference_no`` — the Medusa
		``payment_id`` is used as the reference so a duplicate
		``payment.captured`` webhook doesn't create a second Payment Entry."""
		if not sales_invoice or not self.settings or not self.settings.get("cash_bank_account"):
			return None
		if flt(sales_invoice.grand_total) <= 0:
			return None
		outstanding = flt(frappe.db.get_value("Sales Invoice", sales_invoice.name, "outstanding_amount"))
		if outstanding <= 0:
			return None
		reference_no = cstr(payment_id) or sales_invoice.name
		existing_pe = frappe.db.get_value(
			"Payment Entry", {"reference_no": reference_no, "docstatus": 1}, "name"
		)
		if existing_pe:
			return existing_pe
		return self._make_payment_entry(
			sales_invoice, posting_date, reference_no=reference_no, order_id=order_id
		)

	def _make_payment_entry(
		self, sales_invoice, posting_date, *, reference_no=None, order_id=None
	) -> str | None:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry(
			sales_invoice.doctype, sales_invoice.name, bank_account=self.settings.cash_bank_account
		)
		pe.flags.ignore_mandatory = True
		pe.reference_no = cstr(reference_no or sales_invoice.name)
		pe.posting_date = posting_date or nowdate()
		pe.reference_date = posting_date or nowdate()
		if order_id and frappe.get_meta("Payment Entry").has_field(ORDER_ID_FIELD):
			pe.set(ORDER_ID_FIELD, order_id)
		pe.insert()
		pe.submit()
		return pe.name
