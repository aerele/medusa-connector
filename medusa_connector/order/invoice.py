# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Sales Invoice from Medusa paid orders.

Payment Entry creation/reconciliation is owned by ``PaymentSync`` — this
class only creates/loads the Sales Invoice itself.
"""

from __future__ import annotations

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from frappe.utils import cint, cstr, flt, getdate, nowdate

from medusa_connector.constants import ORDER_ID_FIELD, ORDER_NUMBER_FIELD, ORDER_STATUS_FIELD


class InvoiceSync:
	"""Creates/loads the Sales Invoice for one Medusa order."""

	def __init__(self, settings):
		self.settings = settings

	def create(self, order: dict, sales_order) -> str | None:
		"""Create and submit the Sales Invoice when settings allow it.
		Idempotent on ``medusa_order_id``. Returns the Sales Invoice name (new
		or existing), or ``None`` when skipped. Does **not** create a Payment
		Entry — pass the result to ``PaymentSync.reconcile`` for that."""
		order_id = cstr(order.get("id") or "")
		if not order_id or not sales_order or not cint(self.settings.get("sync_sales_invoice")):
			return None
		existing_si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id}, "name")
		if existing_si:
			return existing_si
		if sales_order.docstatus != 1:
			return None
		posting_date = self.get_posting_date(order)
		si = make_sales_invoice(sales_order.name)
		si.set(ORDER_ID_FIELD, order_id)
		si.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		si.set(ORDER_STATUS_FIELD, cstr(order.get("payment_status") or order.get("status") or ""))
		si.set_posting_time = 1
		si.posting_date = posting_date
		si.due_date = posting_date
		si.naming_series = self.settings.get("sales_invoice_series") or "SI-MED-"
		si.flags.ignore_mandatory = True
		if self.settings.get("cost_center"):
			for row in si.items:
				row.cost_center = self.settings.cost_center
		si.insert(ignore_mandatory=True)
		si.submit()
		return si.name

	@staticmethod
	def get_posting_date(order: dict):
		"""Posting date used for both the Sales Invoice and, downstream, its
		Payment Entry — kept as a single helper so the two never drift."""
		return getdate(order.get("created_at")) or nowdate()
