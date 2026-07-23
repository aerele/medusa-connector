# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Medusa -> ERPNext order sync orchestrator.

OrderSync coordinates one Medusa order through the pipeline. sync()/cancel()
keep the existing str|None return contract (other code depends on it); the
actual logic + logging lives in _sync()/_cancel(), which return a result() dict.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import cint, cstr, get_datetime

from medusa_connector.constants import ORDER_ID_FIELD, ORDER_STATUS_FIELD, SETTING_DOCTYPE
from medusa_connector.order._shared import result, status_label
from medusa_connector.order.customer_sync import CustomerSync
from medusa_connector.order.fulfillment import FulfillmentSync
from medusa_connector.order.invoice import InvoiceSync
from medusa_connector.order.payment_sync import PaymentSync
from medusa_connector.order.sales_order_sync import SalesOrderSync
from medusa_connector.utils.logging import create_medusa_log, logged_sync


class OrderSync:
	"""Orchestrates Customer -> Address -> Sales Order -> Payment -> Invoice
	-> Fulfillment for one Medusa order. Instantiate per call."""

	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)
		self.customer = CustomerSync(self.settings)
		self.sales_order = SalesOrderSync(self.settings)
		self.payment = PaymentSync(self.settings)
		self.invoice = InvoiceSync(self.settings)
		self.fulfillment = FulfillmentSync(self.settings)

	# -- public API --------------------------------------------------------
	def sync(self, order: dict, request_id: str | None = None) -> str | None:
		"""Ensure Sales Order exists + apply full lifecycle. Returns SO name or None."""
		res = self._sync(order, request_id=request_id)
		return (res or {}).get("sales_order")

	def cancel(self, order: dict, request_id: str | None = None) -> str | None:
		"""Handle order.canceled. Returns SO name if one exists, else None."""
		res = self._cancel(order, request_id=request_id)
		return (res or {}).get("sales_order")

	def update_status_fields(self, order: dict) -> None:
		"""Single responsibility: push the Medusa status label onto SO/SI/DN in place."""
		order_id = cstr(order.get("id") or "")
		if not order_id:
			return
		label = status_label(order)
		for doctype in ("Sales Order", "Sales Invoice", "Delivery Note"):
			dt = DocType(doctype)
			(
				frappe.qb.update(dt).set(dt[ORDER_STATUS_FIELD], label).where(dt[ORDER_ID_FIELD] == order_id)
			).run()

	@staticmethod
	def get_sales_order(order_id: str):
		"""Return Sales Order doc for a Medusa order id, or None."""
		return SalesOrderSync.get_sales_order_doc(order_id)

	def sync_old_orders(self) -> dict:
		"""Bulk sync job. Own manual logging (one batch summary + one row per
		order) instead of @logged_sync, since the log shape differs."""
		from medusa_connector.medusa.order import OrderService

		from_date, to_date = self.settings.old_orders_from, self.settings.old_orders_to
		if not from_date or not to_date:
			create_medusa_log(
				status="Invalid",
				message=_("Sync Orders is enabled but From/To dates are missing."),
				method="medusa_connector.order.sync.OrderSync.sync_old_orders",
				make_new=True,
			)
			return {"synced": 0, "created": 0, "failed": 0, "status": "Invalid"}

		from_iso, to_iso = get_datetime(from_date).isoformat(), get_datetime(to_date).isoformat()
		service = OrderService()
		synced = created = failed = processed = 0

		parent_log = create_medusa_log(
			status="Queued",
			method="medusa_connector.order.sync.OrderSync.sync_old_orders",
			message=_("Syncing Medusa orders (full lifecycle) from {0} to {1}").format(from_iso, to_iso),
			request_data={"from": from_iso, "to": to_iso},
			make_new=True,
		)
		parent_log_name = getattr(parent_log, "name", None) or cstr(parent_log)

		for order in service.iter_orders(created_at_gte=from_iso, created_at_lte=to_iso):
			order_id = cstr(order.get("id") or "")
			if not order_id:
				continue
			processed += 1

			had_so = bool(frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}))
			full = service.get_order(order_id) or order

			log = create_medusa_log(
				status="Queued",
				method="medusa_connector.order.sync.OrderSync.sync",
				message=_("Sync order {0} (lifecycle)").format(order_id),
				request_data={"order_id": order_id, "bulk": True},
				make_new=True,
			)
			log_name = getattr(log, "name", None) or cstr(log)

			if self.sync(full, request_id=log_name):
				synced += 1
				if not had_so:
					created += 1
			else:
				failed += 1

		summary = {
			"synced": synced,
			"created": created,
			"updated": max(synced - created, 0),
			"failed": failed,
			"processed": processed,
			"from": from_iso,
			"to": to_iso,
		}
		frappe.flags.request_id = parent_log_name
		create_medusa_log(
			status="Success" if not failed else "Error",
			message=_(
				"Order sync finished: {0} synced ({1} new SO, {2} updated), {3} failed ({4} in range)."
			).format(synced, created, max(synced - created, 0), failed, processed),
			response_data=summary,
			method="medusa_connector.order.sync.OrderSync.sync_old_orders",
		)
		return summary

	@logged_sync("medusa_connector.order.sync.OrderSync.sync")
	def _sync(self, order: dict, request_id: str | None = None) -> dict[str, Any]:
		"""Single responsibility: get-or-create the Sales Order, then apply lifecycle."""
		if not self.settings.enabled:
			return result("invalid", message=_("Medusa Connector is disabled; order not synced."))

		order_id = cstr(order.get("id") or "")
		if not order_id:
			return result("invalid", message=_("Medusa order payload has no id."))

		existing = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}, "name")
		if existing:
			# Fetched fresh below - no extra reload needed for the existing-SO path.
			so = frappe.get_doc("Sales Order", existing)
			created = False
		else:
			so = self._create_sales_order(order)
			# Reload: submit() side effects (docstatus, per_billed) aren't reflected
			# on the in-memory doc returned by create().
			so = frappe.get_doc("Sales Order", so.name)
			created = True

		lifecycle = self._apply_lifecycle(order, so)
		message = _("{0} Sales Order {1} for Medusa order {2}; lifecycle: {3}.").format(
			_("Created") if created else _("Synced"), so.name, order_id, self._lifecycle_summary(lifecycle)
		)
		return result(
			"success",
			sales_order=so.name,
			order_id=order_id,
			created=created,
			lifecycle=lifecycle,
			message=message,
		)

	@logged_sync("medusa_connector.order.sync.OrderSync.cancel")
	def _cancel(self, order: dict, request_id: str | None = None) -> dict[str, Any]:
		"""Single responsibility: reverse payment (if refunded), cancel linked
		Delivery Notes, then cancel the Sales Order if nothing else blocks it.

		A submitted Sales Invoice keeps the SO linked; that case is left for
		payment.refunded to reverse later - only the status field is updated here.
		"""
		order_id = cstr(order.get("id") or "")
		so_name, so_docstatus = frappe.db.get_value(
			"Sales Order", {ORDER_ID_FIELD: order_id}, ["name", "docstatus"]
		) or (None, None)
		if not so_name:
			return result(
				"invalid",
				order_id=order_id,
				message=_("Sales Order does not exist for Medusa order {0}.").format(order_id),
			)

		if self.payment.is_refunded(order):
			from medusa_connector.order.refund import RefundSync

			payment_id = self.payment.first_payment_id(order)
			if payment_id:
				# Deliberately a *separate* log row: the refund is its own event.
				RefundSync(self.settings).process(payment_id=payment_id, request_id=request_id)

		label = status_label(order)
		dn_result = self.fulfillment.cancel_order_delivery_notes(order_id, status_label=label)

		si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name")
		if si:
			frappe.db.set_value("Sales Invoice", si, ORDER_STATUS_FIELD, label, update_modified=False)

		canceled_so = self.sales_order.cancel_if_safe(order, so_name)
		canceled_dn_count = len(dn_result.get("canceled") or [])

		if canceled_so:
			msg = _("Cancelled Sales Order {0} for Medusa order {1} (Delivery Notes canceled: {2}).").format(
				so_name, order_id, canceled_dn_count
			)
		elif so_docstatus == 2:
			msg = _("Sales Order {0} already cancelled for Medusa order {1}.").format(so_name, order_id)
		else:
			msg = _(
				"Medusa order {1} cancelled: Delivery Notes canceled ({2}); Sales Order {0} kept "
				"(a submitted Sales Invoice keeps it linked - refund workflow will reverse it)."
			).format(so_name, order_id, canceled_dn_count)

		return result(
			"success",
			sales_order=so_name,
			order_id=order_id,
			sales_order_canceled=canceled_so,
			delivery_notes=dn_result,
			sales_invoice=si,
			message=msg,
		)

	# -- internals -----------------------------------------------------------

	def _create_sales_order(self, order: dict):
		"""Single responsibility: Customer + Address resolution, then Sales Order creation."""
		customer_context = self.customer.sync(order)
		self.sales_order.ensure_items(order)
		return self.sales_order.create(
			order,
			customer=customer_context["customer"],
			addresses={
				"billing_address": customer_context["billing_address"],
				"shipping_address": customer_context["shipping_address"],
			},
		)

	def _apply_lifecycle(self, order: dict, sales_order) -> dict[str, Any]:
		"""Single responsibility: project Medusa order state onto documents linked
		to sales_order. Never (re)creates the Sales Order itself. A raised
		exception here propagates to _sync's decorator, which logs it as Error.
		"""
		lifecycle: dict[str, Any] = {
			"sales_invoice": None,
			"delivery_notes": [],
			"order_canceled": False,
			"payment_status": cstr(order.get("payment_status") or ""),
			"fulfillment_status": cstr(order.get("fulfillment_status") or ""),
			"order_status": cstr(order.get("status") or ""),
		}

		self.update_status_fields(order)

		if sales_order.docstatus == 2:
			lifecycle["skipped"] = "sales_order_cancelled"
			return lifecycle

		if (
			sales_order.docstatus == 1
			and self.payment.is_paid(order)
			and cint(self.settings.get("sync_sales_invoice"))
		):
			si_name = self.invoice.create(order, sales_order)
			lifecycle["sales_invoice"] = si_name
			if si_name:
				si = frappe.get_doc("Sales Invoice", si_name)
				self.payment.reconcile(
					si,
					InvoiceSync.get_posting_date(order),
					payment_id=self.payment.captured_payment_id(order),
					order_id=cstr(order.get("id")),
				)
			# Invoice creation can update the SO (e.g. per_billed) - reload before continuing.
			sales_order = frappe.get_doc("Sales Order", sales_order.name)

		if sales_order.docstatus == 1 and cint(self.settings.get("sync_delivery_note")):
			ful_result = self.fulfillment.sync_for_order(order, sales_order)
			lifecycle["delivery_notes"] = ful_result.get("delivery_notes") or []
			lifecycle["canceled_fulfillments"] = ful_result.get("canceled") or []

		if self._is_canceled(order):
			order_id = cstr(order.get("id") or "")
			dn_result = self.fulfillment.cancel_order_delivery_notes(
				order_id, status_label=status_label(order)
			)
			lifecycle["canceled_delivery_notes"] = dn_result.get("canceled") or []
			lifecycle["order_canceled"] = self.sales_order.cancel_if_safe(order, sales_order.name)
			self.update_status_fields(order)

		if self.payment.is_refunded(order):
			lifecycle["refund_status"] = cstr(order.get("payment_status") or "refunded")

		return lifecycle

	@staticmethod
	def _is_canceled(order: dict) -> bool:
		return cstr(order.get("status") or "").lower() in {"canceled", "cancelled"}

	@staticmethod
	def _lifecycle_summary(lifecycle: dict | None) -> str:
		"""Single responsibility: render the lifecycle dict as one log-friendly line."""
		if not lifecycle:
			return "-"
		bits = []
		if lifecycle.get("sales_invoice"):
			bits.append(f"SI={lifecycle['sales_invoice']}")
		if lifecycle.get("delivery_notes"):
			bits.append(f"DNx{len(lifecycle['delivery_notes'])}")
		if lifecycle.get("order_canceled"):
			bits.append("SO cancelled")
		if lifecycle.get("refund_status"):
			bits.append(f"refund={lifecycle['refund_status']}")
		ps, fs = lifecycle.get("payment_status"), lifecycle.get("fulfillment_status")
		if ps or fs:
			bits.append(f"state={ps or '-'}/{fs or '-'}")
		return ", ".join(bits) if bits else "status only"


# ---------------------------------------------------------------------------
# Scheduler entry point (dotted path referenced from hooks.py)
# ---------------------------------------------------------------------------
def sync_old_orders() -> dict | None:
	settings = frappe.get_single(SETTING_DOCTYPE)
	if not settings.enabled or not cint(settings.sync_old_orders):
		return None
	try:
		return OrderSync(settings).sync_old_orders()
	finally:
		frappe.db.set_single_value(SETTING_DOCTYPE, "sync_old_orders", 0)
