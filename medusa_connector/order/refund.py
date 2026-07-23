# Copyright (c) 2026, Aerele and contributors
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
from frappe.utils import cstr

from medusa_connector.constants import ORDER_ID_FIELD, ORDER_STATUS_FIELD, REFUND_ID_FIELD, SETTING_DOCTYPE
from medusa_connector.order._shared import cancel_doc, result
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
			return result("skipped", message=_("Medusa Connector is disabled"))
		payment_id = cstr(payment_id or "")
		if not payment_id:
			return result("invalid", message=_("Missing payment id"))

		refund_id, order_id = self._resolve_refund_and_order(payment_id)
		reversal = self._reverse_order_payment(order_id, refund_id)
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
	def _resolve_refund_and_order(payment_id: str) -> tuple[str, str]:
		from medusa_connector.medusa.payment import PaymentService

		payment = PaymentService().get_payment(payment_id) or {}
		refunds = payment.get("refunds") or []
		refund_id = (
			cstr(refunds[-1].get("id"))
			if refunds and isinstance(refunds[-1], dict) and refunds[-1].get("id")
			else f"{payment_id}-refund"
		)
		order_id = PaymentService().resolve_order_id({"id": payment_id}, payment_id=payment_id)
		return refund_id, cstr(order_id or "")

	@staticmethod
	def _reverse_order_payment(order_id: str, refund_id: str) -> dict[str, Any]:
		if not order_id:
			return {
				"status": "invalid",
				"sales_invoice": None,
				"payment_entry": None,
				"message": _("No Medusa order linked to the refunded payment."),
			}
		existing_si = frappe.db.get_value(
			"Sales Invoice", {REFUND_ID_FIELD: refund_id, "docstatus": ["in", [1, 2]]}, "name"
		)
		if existing_si:
			return {
				"status": "skipped",
				"sales_invoice": existing_si,
				"payment_entry": frappe.db.get_value("Payment Entry", {REFUND_ID_FIELD: refund_id}, "name"),
				"message": _("Refund {0} already applied.").format(refund_id),
			}
		si_name = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name")
		if not si_name:
			return {
				"status": "skipped",
				"sales_invoice": None,
				"payment_entry": None,
				"message": _("No submitted Sales Invoice for Medusa order {0}; nothing to reverse.").format(
					order_id
				),
			}
		pe_name = RefundSync._find_payment_entry(order_id, si_name)
		if frappe.get_meta("Sales Invoice").has_field(REFUND_ID_FIELD):
			frappe.db.set_value("Sales Invoice", si_name, REFUND_ID_FIELD, refund_id, update_modified=False)
		if pe_name and frappe.get_meta("Payment Entry").has_field(REFUND_ID_FIELD):
			frappe.db.set_value("Payment Entry", pe_name, REFUND_ID_FIELD, refund_id, update_modified=False)
		if pe_name:
			cancel_doc("Payment Entry", pe_name)
		cancel_doc("Sales Invoice", si_name)
		frappe.db.set_value("Sales Invoice", si_name, ORDER_STATUS_FIELD, "refunded", update_modified=False)
		return {
			"status": "success",
			"sales_invoice": si_name,
			"payment_entry": pe_name,
			"message": _("Refund {0}: reversed Sales Invoice {1} and Payment Entry {2}.").format(
				refund_id or "-", si_name, pe_name or "-"
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
