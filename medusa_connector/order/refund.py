# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa payment refund → ERPNext reversal.

Medusa models a refund on a payment (``payment.refunded`` webhook). The matching
ERPNext documents are the **Sales Invoice** (raised on capture) and the
**Payment Entry** (posted against that invoice). A refund reverses both using
ERPNext's standard cancellation flow:

  Payment Entry  →  cancel()        (reverses the receipt)
  Sales Invoice  →  cancel()        (reverses the receivable)

Cancellation is applied per the per-order decision captured on
``payment.refunded``: both full and partial refunds cancel the linked SI + PE,
which mirrors the chosen connector convention and keeps refund handling
symmetric and idempotent. Reverse-order (PE first, then SI) respects ERPNext
link constraints.

Idempotency
-----------
The Medusa refund id is stamped on both reversed documents
(``medusa_refund_id``). A re-delivered ``payment.refunded`` webhook for the
same refund id short-circuits without re-processing. The refund id is taken
from the Admin payment's ``refunds`` list (``id`` of the most recent refund) or,
when unavailable, derived deterministically from the payment id.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	ORDER_STATUS_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.utils.logging import create_medusa_log


def process_refund(
	*,
	payment_id: str | None = None,
	refund_id: str | None = None,
	order: dict | None = None,
	request_id: str | None = None,
) -> dict[str, Any]:
	"""Reverse the Sales Invoice and Payment Entry for a refunded Medusa payment.

	Resolves the Medusa order from the payment (re-fetching the Admin payment to
	pick up its ``refunds``), locates the linked SI / PE, and cancels them via
	ERPNext's standard cancellation flow. Safe to call from webhooks and retries.

	Returns a result dict: ``{status, refund_id, payment_id, order_id,
	sales_invoice, payment_entry, message}``.
	"""
	frappe.set_user("Administrator")
	if request_id:
		frappe.flags.request_id = request_id

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled:
		return _result("skipped", message=_("Medusa Connector is disabled"))

	payment_id = cstr(payment_id or "")
	if not payment_id:
		log_name = create_medusa_log(
			status="Invalid",
			message=_("payment.refunded payload has no payment id."),
			method="medusa_connector.order.refund.process_refund",
			make_new=True,
		)
		return _result("invalid", message=_("Missing payment id"), request_id=log_name)

	try:
		refund_id, order = _resolve_refund_and_order(payment_id, refund_id, order)
		order_id = cstr((order or {}).get("id") or "")
		result = _reverse_order_payment(order_id, refund_id)

		message = _format_outcome(payment_id, refund_id, order_id, result)
		create_medusa_log(
			status="Success",
			message=message,
			request_data={"payment_id": payment_id, "refund_id": refund_id, "order_id": order_id},
			response_data=result,
			method="medusa_connector.order.refund.process_refund",
		)
		return _result(
			result.get("status") or "success",
			payment_id=payment_id,
			refund_id=refund_id,
			order_id=order_id,
			sales_invoice=result.get("sales_invoice"),
			payment_entry=result.get("payment_entry"),
			message=message,
		)
	except Exception as exc:
		create_medusa_log(
			status="Error",
			exception=exc,
			rollback=True,
			request_data={"payment_id": payment_id, "refund_id": refund_id},
			method="medusa_connector.order.refund.process_refund",
		)
		return _result("error", payment_id=payment_id, refund_id=refund_id, message=str(exc))


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


def _reverse_order_payment(order_id: str, refund_id: str) -> dict[str, Any]:
	"""Cancel the Payment Entry then the Sales Invoice linked to ``order_id``.

	Reverse order respects ERPNext link constraints (PE references SI). Each
	step is guarded so a failure on one document is reported without masking
	the other. ``refund_id`` is the idempotency key: if it is already stamped
	on a cancelled SI/PE, the refund is treated as already applied.
	"""
	result: dict[str, Any] = {
		"status": "success",
		"refund_id": refund_id,
		"order_id": order_id,
		"sales_invoice": None,
		"payment_entry": None,
		"already_processed": False,
	}
	if not order_id:
		result["status"] = "invalid"
		result["message"] = _("No Medusa order linked to the refunded payment.")
		return result

	# Idempotency: this refund was already applied to an SI (and its PE).
	existing_si = frappe.db.get_value(
		"Sales Invoice", {REFUND_ID_FIELD: refund_id, "docstatus": ["in", [1, 2]]}, "name"
	)
	if existing_si:
		result["already_processed"] = True
		result["sales_invoice"] = existing_si
		result["payment_entry"] = frappe.db.get_value("Payment Entry", {REFUND_ID_FIELD: refund_id}, "name")
		result["status"] = "skipped"
		result["message"] = _("Refund {0} already applied.").format(refund_id)
		return result

	si_name = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name")
	if not si_name:
		result["status"] = "skipped"
		result["message"] = _("No submitted Sales Invoice for Medusa order {0}; nothing to reverse.").format(
			order_id
		)
		return result

	# Payment Entry linked to this order / invoice. PE created on capture uses the
	# Medusa payment id as ``reference_no``; ``medusa_order_id`` is also stamped.
	pe_name = _find_payment_entry(order_id, si_name)

	# Stamp the refund id on both documents BEFORE cancelling so a crash mid-way
	# still leaves an idempotency trail. ``set_value`` is committed independently
	# of the (separately committed) cancellation below.
	if refund_id and frappe.get_meta("Sales Invoice").has_field(REFUND_ID_FIELD):
		frappe.db.set_value("Sales Invoice", si_name, REFUND_ID_FIELD, refund_id, update_modified=False)
	if pe_name and refund_id and frappe.get_meta("Payment Entry").has_field(REFUND_ID_FIELD):
		frappe.db.set_value("Payment Entry", pe_name, REFUND_ID_FIELD, refund_id, update_modified=False)

	status_label = "refunded"

	# 1) Reverse the Payment Entry first (it references the SI).
	pe_canceled = False
	if pe_name:
		pe_canceled = _cancel_doc("Payment Entry", pe_name)
		result["payment_entry"] = pe_name

	# 2) Reverse the Sales Invoice.
	si_canceled = _cancel_doc("Sales Invoice", si_name)
	result["sales_invoice"] = si_name
	frappe.db.set_value("Sales Invoice", si_name, ORDER_STATUS_FIELD, status_label, update_modified=False)

	if not si_canceled:
		result["status"] = "partial"
		result["message"] = _(
			"Refund {0}: Payment Entry {1} reversed but Sales Invoice {2} could not be cancelled."
		).format(refund_id or "-", pe_name or "-", si_name)
	elif pe_name and not pe_canceled:
		result["status"] = "partial"
		result["message"] = _(
			"Refund {0}: Sales Invoice {1} cancelled but Payment Entry {2} could not be reversed."
		).format(refund_id or "-", si_name, pe_name)
	else:
		result["message"] = _("Refund {0}: reversed Sales Invoice {1} and Payment Entry {2}.").format(
			refund_id or "-", si_name, pe_name or "-"
		)
	return result


def _find_payment_entry(order_id: str, sales_invoice_name: str) -> str | None:
	"""Locate the submitted Payment Entry for an order/invoice.

	Preference: PE stamped with ``medusa_order_id`` (created by capture flow),
	else any submitted PE referencing this Sales Invoice via its references.
	"""
	pe = frappe.db.get_value(
		"Payment Entry",
		{ORDER_ID_FIELD: order_id, "docstatus": 1, "payment_type": "Receive"},
		"name",
	)
	if pe:
		return pe

	# Fall back to the PE linked to the SI through Payment Entry Reference.
	pe = frappe.db.get_value(
		"Payment Entry Reference",
		{"reference_doctype": "Sales Invoice", "reference_name": sales_invoice_name},
		"parent",
	)
	if pe and frappe.db.get_value("Payment Entry", pe, "docstatus") == 1:
		return pe
	return None


def _cancel_doc(doctype: str, name: str) -> bool:
	"""Cancel a submitted ERPNext document via its standard ``cancel()`` flow.

	Returns True if cancelled (or already cancelled), False on failure. Errors
	are logged but not raised so the companion document can still be reversed.
	"""
	try:
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 2:
			return True
		if doc.docstatus != 1:
			return False
		doc.cancel()
		return True
	except Exception as exc:
		frappe.logger("medusa_connector").warning(f"Could not cancel {doctype} {name} for refund: {exc}")
		return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _resolve_refund_and_order(
	payment_id: str, refund_id: str | None, order: dict | None
) -> tuple[str, dict | None]:
	"""Fetch the Medusa payment to recover the latest refund id and order.

	The webhook body only carries ``{"id": "pay_..."}``; the refund id and the
	linked order come from the Admin payment's ``refunds`` and
	``payment_collection.order`` (see ``PaymentService``).
	"""
	from medusa_connector.medusa.payment import PaymentService

	entity = order if isinstance(order, dict) else None

	if not refund_id or not entity:
		payment = PaymentService().get_payment(payment_id) or {}
		if not refund_id:
			refund_id = _latest_refund_id(payment) or f"{payment_id}-refund"
		if not entity:
			entity = None  # order resolved through PaymentService below

	# Resolve the order id from the payment when not supplied by the caller.
	order_id = ""
	if isinstance(entity, dict) and entity.get("id"):
		order_id = cstr(entity.get("id"))
	if not order_id:
		order_id = PaymentService().resolve_order_id({"id": payment_id}, payment_id=payment_id)

	if order_id and not (isinstance(entity, dict) and entity.get("id")):
		from medusa_connector.medusa.order import OrderService

		entity = OrderService().get_order(order_id) or {"id": order_id}

	return cstr(refund_id), entity


def _latest_refund_id(payment: dict) -> str | None:
	"""Most recent refund id on a Medusa payment (``refunds[].id``)."""
	refunds = payment.get("refunds") or []
	if not isinstance(refunds, list):
		return None
	latest: str | None = None
	for refund in refunds:
		if not isinstance(refund, dict):
			continue
		rid = cstr(refund.get("id") or "")
		if rid:
			latest = rid  # list is oldest→newest; keep the last seen
	return latest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_outcome(payment_id: str, refund_id: str, order_id: str, result: dict[str, Any]) -> str:
	status = result.get("status") or "success"
	if status == "skipped" and result.get("already_processed"):
		return _("Refund {0} for payment {1} already processed (order {2}).").format(
			refund_id or "-", payment_id, order_id or "-"
		)
	if status == "skipped":
		return result.get("message") or _("Nothing to reverse for payment {0}.").format(payment_id)
	if status == "partial":
		return result.get("message") or _("Partial reversal for payment {0}.").format(payment_id)
	return _("Refunded payment {0} (refund {1}, order {2}): SI {3}, PE {4}.").format(
		payment_id,
		refund_id or "-",
		order_id or "-",
		result.get("sales_invoice") or "-",
		result.get("payment_entry") or "-",
	)


def _result(status: str, **kwargs) -> dict[str, Any]:
	out: dict[str, Any] = {"status": status}
	out.update({k: v for k, v in kwargs.items() if v is not None})
	return out
