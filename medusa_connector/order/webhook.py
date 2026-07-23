# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Order-domain webhook handlers -> ERPNext Sales Order sync."""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from medusa_connector.constants import FULFILLMENT_ID_FIELD, ORDER_ID_FIELD, SETTING_DOCTYPE
from medusa_connector.medusa.exceptions import MedusaConnectorError
from medusa_connector.medusa.order import OrderService
from medusa_connector.medusa.payment import PaymentService
from medusa_connector.order._shared import result
from medusa_connector.order.fulfillment import FulfillmentSync
from medusa_connector.order.invoice import InvoiceSync
from medusa_connector.order.payment_sync import PaymentSync
from medusa_connector.order.refund import RefundSync
from medusa_connector.order.sync import OrderSync
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


class OrderBaseHandler(BaseHandler):
	"""Common functionality for handlers operating on Medusa orders."""

	resource = "orders"

	def get_order(self, event: MedusaEvent, entity: dict) -> dict:
		order = entity if isinstance(entity, dict) else {}
		order_id = self.get_order_id(event, order)
		if order_id and (not order.get("items") or not self._has_customer_ref(order)):
			order = OrderService().get_order(order_id) or order
		return order

	@staticmethod
	def _has_customer_ref(order: dict) -> bool:
		return bool(order.get("customer_id") or (order.get("customer") or {}).get("id"))

	def get_order_id(self, event: MedusaEvent, order: dict) -> str:
		return cstr(order.get("id") or event.entity_id or "")

	def get_display_id(self, event: MedusaEvent, order: dict) -> str:
		return cstr(order.get("display_id") or order.get("id") or event.entity_id or "")

	def get_order_sync(self) -> OrderSync:
		return OrderSync()

	def get_existing_sales_order(self, event: MedusaEvent, order: dict, sync: OrderSync | None = None):
		order_id = self.get_order_id(event, order)
		if not order_id:
			return None
		sync = sync or self.get_order_sync()
		return sync.get_sales_order(order_id)

	def sync_order(self, event: MedusaEvent, order: dict, sync: OrderSync | None = None) -> str | None:
		sync = sync or self.get_order_sync()
		return sync.sync(order, request_id=event.log_name)

	def cancel_order(self, event: MedusaEvent, order: dict, sync: OrderSync | None = None) -> str | None:
		sync = sync or self.get_order_sync()
		return sync.cancel(order, request_id=event.log_name)

	def format_order_sync_result(self, event: MedusaEvent, order: dict, so_name: str | None) -> dict:
		"""Turn a Sales Order name (or None) into a proper result dict — this
		is what used to be a plain formatted string, silently hiding failure."""
		display_id = self.get_display_id(event, order)
		if so_name:
			return result(
				"success",
				sales_order=so_name,
				message=f"{event.name}: Sales Order {so_name} for order #{display_id}",
			)
		return result(
			"invalid",
			message=f"{event.name}: order #{display_id} not created (see Ecommerce Integration Log)",
		)


@register("order.placed")
class OrderPlacedHandler(OrderBaseHandler):
	def process(self, event: MedusaEvent, entity: dict) -> dict:
		order = self.get_order(event, entity)
		so_name = self.sync_order(event, order)
		return self.format_order_sync_result(event, order, so_name)


@register("order.updated", "order.completed")
class OrderUpdatedHandler(OrderBaseHandler):
	def process(self, event: MedusaEvent, entity: dict) -> dict:
		order = self.get_order(event, entity)
		sync = self.get_order_sync()
		existing = self.get_existing_sales_order(event, order, sync)
		if existing:
			sync.update_status_fields(order)
			return result(
				"success",
				sales_order=existing.name,
				message=f"{event.name}: updated status on {existing.name}",
			)
		so_name = self.sync_order(event, order, sync)
		return self.format_order_sync_result(event, order, so_name)


@register("order.canceled")
class OrderCanceledHandler(OrderBaseHandler):
	def process(self, event: MedusaEvent, entity: dict) -> dict:
		order = self.get_order(event, entity)
		so_name = self.cancel_order(event, order)
		display_id = self.get_display_id(event, order)
		if so_name:
			return result(
				"success",
				sales_order=so_name,
				message=f"{event.name}: handled Sales Order {so_name} for order #{display_id}",
			)
		return result("invalid", message=f"{event.name}: no Sales Order for order #{display_id}")


@register("order.fulfillment_created", "order.fulfillment_canceled", "fulfillment.canceled")
class FulfillmentHandler(BaseHandler):
	"""Medusa v2 fulfillment payloads contain ``order_id`` and
	``fulfillment_id``; the event ``entity_id`` refers to the fulfillment."""

	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> dict:
		entity = entity if isinstance(entity, dict) else {}
		order_id = cstr(entity.get("order_id") or "")
		fulfillment_id = cstr(entity.get("fulfillment_id") or entity.get("id") or event.entity_id or "")
		is_cancel = event.name in {"order.fulfillment_canceled", "fulfillment.canceled"}
		sync = FulfillmentSync()
		if is_cancel:
			res = sync.cancel(
				fulfillment_id=fulfillment_id, order_id=order_id or None, request_id=event.log_name
			)
		else:
			res = sync.sync(
				order_id=order_id or None, fulfillment_id=fulfillment_id or None, request_id=event.log_name
			)
		# res is FulfillmentSync's real result() dict — status is preserved,
		# only the message is reformatted. This is the exact fix for a
		# connection error showing "Success": res["status"] is now "error"
		# and that survives into the log instead of being discarded.
		message = (
			f"{event.name}: {res.get('status')} DN={res.get('delivery_note') or '-'} "
			f"fulfillment={fulfillment_id or '-'} order={order_id or res.get('order_id') or '-'} "
			f"— {res.get('message') or ''}"
		).strip()
		return {**res, "message": message}


@register("shipment.created", "delivery.created", "order.shipment_created")
class ShipmentHandler(BaseHandler):
	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> dict:
		entity = entity if isinstance(entity, dict) else {}
		fulfillment_id = cstr(entity.get("id") or entity.get("fulfillment_id") or event.entity_id or "")
		order_id = cstr(entity.get("order_id") or "")
		if fulfillment_id and not order_id:
			order_id = cstr(
				frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, ORDER_ID_FIELD)
				or ""
			)
		if not fulfillment_id:
			return result("invalid", message=f"{event.name}: missing fulfillment id on payload")
		if not order_id:
			return result(
				"skipped",
				message=(
					f"{event.name}: fulfillment {fulfillment_id} — no order_id and no existing DN; "
					"wait for order.fulfillment_created or include order_id"
				),
			)
		res = FulfillmentSync().sync(
			order_id=order_id, fulfillment_id=fulfillment_id, request_id=event.log_name
		)
		message = (
			f"{event.name}: {res.get('status')} DN={res.get('delivery_note') or '-'} "
			f"fulfillment={fulfillment_id} order={order_id} — {res.get('message') or ''}"
		).strip()
		return {**res, "message": message}


@register("payment.captured", "payment.refunded")
class PaymentHandler(BaseHandler):
	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> dict:
		entity = entity if isinstance(entity, dict) else {}
		payment_id = cstr(entity.get("id") or event.entity_id or "")
		if event.name == "payment.refunded":
			return self._handle_refund(event, payment_id)
		return self._handle_payment_capture(event, entity, payment_id)

	def _handle_refund(self, event: MedusaEvent, payment_id: str) -> dict:
		res = RefundSync().process(payment_id=payment_id, request_id=event.log_name)
		message = (
			f"{event.name}: {res.get('status')} refund={res.get('refund_id') or '-'} "
			f"payment={payment_id} order={res.get('order_id') or '-'} "
			f"SI={res.get('sales_invoice') or '-'} PE={res.get('payment_entry') or '-'} "
			f"— {res.get('message') or ''}"
		).strip()
		return {**res, "message": message}

	def _handle_payment_capture(self, event: MedusaEvent, entity: dict, payment_id: str) -> dict:
		try:
			order_id = PaymentService().resolve_order_id(entity, payment_id=payment_id)
		except MedusaConnectorError as exc:
			return result(
				"error", message=f"{event.name}: could not resolve order for payment {payment_id} ({exc})"
			)
		if not order_id:
			return result(
				"invalid", message=f"{event.name}: no order linked to payment {payment_id or '(unknown)'}"
			)
		order = OrderService().get_order(order_id)
		if not order:
			return result(
				"error", message=f"{event.name}: could not load order {order_id} for payment {payment_id}"
			)
		settings = frappe.get_doc(SETTING_DOCTYPE)
		sync = OrderSync(settings)
		so_name = sync.sync(order, request_id=event.log_name)
		if not so_name:
			return result(
				"invalid", message=f"{event.name}: payment {payment_id} order {order_id} — SO not created"
			)
		so = frappe.get_doc("Sales Order", so_name)
		si_name = InvoiceSync(settings).create(order, so)
		if not si_name:
			return result(
				"skipped",
				sales_order=so_name,
				message=f"{event.name}: SO {so_name} (invoice skipped or already billed; payment {payment_id}, order {order_id})",
			)
		si = frappe.get_doc("Sales Invoice", si_name)
		PaymentSync(settings).reconcile(
			si, InvoiceSync.get_posting_date(order), payment_id=payment_id or None, order_id=order_id
		)
		return result(
			"success",
			sales_order=so_name,
			sales_invoice=si_name,
			message=f"{event.name}: Sales Invoice {si_name} for {so_name} (payment {payment_id}, order {order_id})",
		)


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> dict:
		return result("skipped", message=f"{event.name}: return {event.entity_id} (sync pending)")
