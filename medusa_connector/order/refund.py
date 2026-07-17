# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Medusa payment refund → ERPNext reversal.

  Payment Entry  →  cancel()   (reverses the receipt)
  Sales Invoice  →  cancel()   (reverses the receivable)

The Medusa refund id is stamped on both documents (``medusa_refund_id``);
a re-delivered ``payment.refunded`` webhook for the same refund id is a no-op.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, flt

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
	MedusaOperationStatus,
)
from medusa_connector.order._shared import result
from medusa_connector.utils.logging import logged_sync


class RefundSync:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)

	@logged_sync("medusa_connector.order.refund.RefundSync.process")
	def process(self, *, payment_id: str, request_id: str | None = None) -> dict[str, Any]:
		"""Reverse the Sales Invoice and Payment Entry for a refunded Medusa
		payment. Returns ``{status, refund_id, payment_id, order_id,
		sales_invoice, payment_entry, message}``."""
		if not self.settings.enabled:
			return result(MedusaOperationStatus.SKIPPED, message=_("Medusa Connector is disabled"))
		payment_id = cstr(payment_id or "")
		if not payment_id:
			return result(MedusaOperationStatus.ERROR, message=_("Missing payment id"))

		from medusa_connector.medusa.payment import PaymentService

		payment = PaymentService().get_payment(payment_id) or {}
		order_id = cstr(PaymentService().resolve_order_id({"id": payment_id}, payment_id=payment_id) or "")
		refunds = [row for row in payment.get("refunds") or [] if isinstance(row, dict)]
		if not refunds:
			refunds = [
				{
					"id": f"{payment_id}-refund",
					"amount": payment.get("refunded_amount") or payment.get("amount"),
				}
			]
		reversal = {}
		refund_id = ""
		for refund in refunds:
			refund_id = cstr(refund.get("id") or f"{payment_id}-refund")
			reversal = self._reverse_order_payment(order_id, refund_id, flt(refund.get("amount") or 0))
		return result(
			reversal["status"],
			payment_id=payment_id,
			refund_id=refund_id,
			order_id=order_id,
			sales_invoice=reversal.get("sales_invoice"),
			payment_entry=reversal.get("payment_entry"),
			message=reversal["message"],
		)

	@staticmethod
	def _resolve_refund_and_order(payment_id: str) -> tuple[str, str, float]:
		from medusa_connector.medusa.payment import PaymentService

		payment = PaymentService().get_payment(payment_id) or {}
		refunds = payment.get("refunds") or []
		refund_id = (
			cstr(refunds[-1].get("id"))
			if refunds and isinstance(refunds[-1], dict) and refunds[-1].get("id")
			else f"{payment_id}-refund"
		)
		refund = refunds[-1] if refunds and isinstance(refunds[-1], dict) else {}
		order_id = PaymentService().resolve_order_id({"id": payment_id}, payment_id=payment_id)
		return refund_id, cstr(order_id or ""), flt(refund.get("amount") or 0)

	def _reverse_order_payment(self, order_id: str, refund_id: str, amount: float) -> dict[str, Any]:
		if not order_id:
			return {
				"status": MedusaOperationStatus.ERROR.value,
				"message": _("No Medusa order linked to the refund."),
			}
		existing = frappe.db.get_value("Sales Invoice", {REFUND_ID_FIELD: refund_id, "is_return": 1}, "name")
		if existing:
			return {
				"status": MedusaOperationStatus.SKIPPED.value,
				"sales_invoice": existing,
				"payment_entry": frappe.db.get_value("Payment Entry", {REFUND_ID_FIELD: refund_id}, "name"),
				"message": _("Refund {0} already applied.").format(refund_id),
			}
		si_name = frappe.db.get_value(
			"Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0}, "name"
		)
		if not si_name:
			return {
				"status": MedusaOperationStatus.SKIPPED.value,
				"sales_invoice": None,
				"payment_entry": None,
				"message": _("No submitted Sales Invoice for Medusa order {0}.").format(order_id),
			}
		from erpnext.accounts.doctype.sales_invoice.mapper import make_sales_return

		original = frappe.get_doc("Sales Invoice", si_name)
		credit = make_sales_return(si_name)
		# Scale by *rate*, not qty: qty must stay a whole number for UOMs that
		# enforce it (e.g. "Nos"), and a partial monetary refund doesn't imply
		# a partial quantity return. make_sales_return() already negates qty to
		# the full original quantity, so we only need to bring the note's value
		# down to the refunded amount.
		ratio = (
			min(abs(flt(amount)) / abs(flt(original.grand_total)), 1)
			if flt(amount) and flt(original.grand_total)
			else 1
		)
		for row in credit.items:
			row.rate = flt(row.rate) * ratio
		credit.set(ORDER_ID_FIELD, order_id)
		credit.set(REFUND_ID_FIELD, refund_id)
		credit.naming_series = self.settings.get("sales_invoice_series") or "SI-MED-"
		credit.flags.ignore_mandatory = True
		credit.insert(ignore_mandatory=True)
		credit.submit()
		pe_name = None
		if self.settings.get("cash_bank_account"):
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			pe = get_payment_entry("Sales Invoice", credit.name, bank_account=self.settings.cash_bank_account)
			pe.reference_no = refund_id
			pe.reference_date = credit.posting_date
			pe.set(ORDER_ID_FIELD, order_id)
			pe.set(REFUND_ID_FIELD, refund_id)
			pe.insert()
			pe.submit()
			pe_name = pe.name
		return {
			"status": MedusaOperationStatus.SUCCESS.value,
			"sales_invoice": credit.name,
			"payment_entry": pe_name,
			"message": _("Refund {0}: created Credit Note {1} and Refund Payment {2}.").format(
				refund_id, credit.name, pe_name or "-"
			),
		}

	@staticmethod
	def _find_payment_entry(order_id: str, sales_invoice_name: str) -> str | None:
		pe = frappe.db.get_value(
			"Payment Entry", {ORDER_ID_FIELD: order_id, "docstatus": 1, "payment_type": "Receive"}, "name"
		)
		if pe:
			return pe
		pe = frappe.db.get_value(
			"Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": sales_invoice_name},
			"parent",
		)
		if pe and frappe.db.get_value("Payment Entry", pe, "docstatus") == 1:
			return pe
		return None
