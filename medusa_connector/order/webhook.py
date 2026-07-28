# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.utils import cstr

from medusa_connector.constants import FULFILLMENT_ID_FIELD, ORDER_ID_FIELD, SETTING_DOCTYPE
from medusa_connector.medusa.order import OrderService
from medusa_connector.medusa.payment import PaymentService
from medusa_connector.order._shared import result
from medusa_connector.order.fulfillment import FulfillmentSync
from medusa_connector.order.invoice import InvoiceSync
from medusa_connector.order.payment_sync import PaymentSync
from medusa_connector.order.refund import RefundSync
from medusa_connector.order.sync import OrderSync
from medusa_connector.webhook.dispatch import MedusaEvent
from medusa_connector.webhook.registry import register


class OrderBase:
	resource = "orders"

	def get_order(self, event: MedusaEvent) -> dict:
		order = event.data if isinstance(event.data, dict) else {}
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

	def get_existing_sales_order(
		self,
		event: MedusaEvent,
		order: dict,
		sync: OrderSync | None = None,
	):
		order_id = self.get_order_id(event, order)

		if not order_id:
			return None

		sync = sync or self.get_order_sync()
		return sync.get_sales_order(order_id)

	def sync_order(
		self,
		event: MedusaEvent,
		order: dict,
		sync: OrderSync | None = None,
	) -> str | None:
		sync = sync or self.get_order_sync()
		return sync.sync(order, request_id=event.log_name)

	def cancel_order(
		self,
		event: MedusaEvent,
		order: dict,
		sync: OrderSync | None = None,
	) -> str | None:
		sync = sync or self.get_order_sync()
		return sync.cancel(order, request_id=event.log_name)

	def format_order_result(
		self,
		event: MedusaEvent,
		order: dict,
		so_name: str | None,
	) -> dict:
		display_id = self.get_display_id(event, order)

		if so_name:
			return result(
				"success",
				sales_order=so_name,
				message=f"{event.name}: Sales Order {so_name} for order #{display_id}",
			)

		return result(
			"invalid",
			message=f"{event.name}: order #{display_id} not created",
		)


@register("order.placed")
class OrderPlacedHandler(OrderBase):
	def handle(self, event: MedusaEvent) -> dict:
		order = self.get_order(event)
		so_name = self.sync_order(event, order)
		return self.format_order_result(event, order, so_name)


@register("order.updated", "order.completed")
class OrderUpdatedHandler(OrderBase):
	def handle(self, event: MedusaEvent) -> dict:
		order = self.get_order(event)
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
		return self.format_order_result(event, order, so_name)


@register("order.canceled")
class OrderCanceledHandler(OrderBase):
	def handle(self, event: MedusaEvent) -> dict:
		order = self.get_order(event)
		so_name = self.cancel_order(event, order)
		display_id = self.get_display_id(event, order)

		if so_name:
			return result(
				"success",
				sales_order=so_name,
				message=f"{event.name}: handled Sales Order {so_name} for order #{display_id}",
			)

		return result(
			"invalid",
			message=f"{event.name}: no Sales Order for order #{display_id}",
		)


@register("order.fulfillment_created", "order.fulfillment_canceled", "fulfillment.canceled")
class FulfillmentHandler:
	def handle(self, event: MedusaEvent) -> dict:
		entity = event.data if isinstance(event.data, dict) else {}
		order_id = cstr(entity.get("order_id") or "")
		fulfillment_id = cstr(entity.get("fulfillment_id") or entity.get("id") or event.entity_id or "")

		if not fulfillment_id:
			return result(
				"invalid",
				message=f"{event.name}: missing fulfillment id",
			)

		sync = FulfillmentSync()

		if event.name in {"order.fulfillment_canceled", "fulfillment.canceled"}:
			res = sync.cancel(
				fulfillment_id=fulfillment_id,
				order_id=order_id or None,
				request_id=event.log_name,
			)
		else:
			res = sync.sync(
				order_id=order_id or None,
				fulfillment_id=fulfillment_id,
				request_id=event.log_name,
			)

		return {
			**res,
			"message": (
				f"{event.name}: {res.get('status')} "
				f"DN={res.get('delivery_note') or '-'} "
				f"fulfillment={fulfillment_id} "
				f"order={order_id or res.get('order_id') or '-'} "
				f"— {res.get('message') or ''}"
			).strip(),
		}


@register("shipment.created", "delivery.created", "order.shipment_created")
class ShipmentHandler:
	def handle(self, event: MedusaEvent) -> dict:
		entity = event.data if isinstance(event.data, dict) else {}
		fulfillment_id = cstr(entity.get("id") or entity.get("fulfillment_id") or event.entity_id or "")
		order_id = cstr(entity.get("order_id") or "")

		if fulfillment_id and not order_id:
			order_id = cstr(
				frappe.db.get_value(
					"Delivery Note",
					{FULFILLMENT_ID_FIELD: fulfillment_id},
					ORDER_ID_FIELD,
				)
				or ""
			)

		if not fulfillment_id:
			return result(
				"invalid",
				message=f"{event.name}: missing fulfillment id",
			)

		if not order_id:
			return result(
				"skipped",
				message=(
					f"{event.name}: fulfillment {fulfillment_id} has no order_id "
					"and no existing Delivery Note"
				),
			)

		res = FulfillmentSync().sync(
			order_id=order_id,
			fulfillment_id=fulfillment_id,
			request_id=event.log_name,
		)

		return {
			**res,
			"message": (
				f"{event.name}: {res.get('status')} "
				f"DN={res.get('delivery_note') or '-'} "
				f"fulfillment={fulfillment_id} "
				f"order={order_id} "
				f"— {res.get('message') or ''}"
			).strip(),
		}


@register("payment.captured", "payment.refunded")
class PaymentHandler:
	def handle(self, event: MedusaEvent) -> dict:
		entity = event.data if isinstance(event.data, dict) else {}
		payment_id = cstr(entity.get("id") or event.entity_id or "")

		if event.name == "payment.refunded":
			return self._handle_refund(event, payment_id)

		return self._handle_capture(event, entity, payment_id)

	def _handle_refund(self, event: MedusaEvent, payment_id: str) -> dict:
		res = RefundSync().process(
			payment_id=payment_id,
			request_id=event.log_name,
		)

		return {
			**res,
			"message": (
				f"{event.name}: {res.get('status')} "
				f"refund={res.get('refund_id') or '-'} "
				f"payment={payment_id or '-'} "
				f"order={res.get('order_id') or '-'} "
				f"SI={res.get('sales_invoice') or '-'} "
				f"PE={res.get('payment_entry') or '-'} "
				f"— {res.get('message') or ''}"
			).strip(),
		}

	def _handle_capture(
		self,
		event: MedusaEvent,
		entity: dict,
		payment_id: str,
	) -> dict:
		try:
			order_id = PaymentService().resolve_order_id(
				entity,
				payment_id=payment_id,
			)
		except Exception as exc:
			return result(
				"error",
				message=(f"{event.name}: could not resolve order for payment {payment_id} ({exc})"),
			)

		if not order_id:
			return result(
				"invalid",
				message=(f"{event.name}: no order linked to payment {payment_id or '(unknown)'}"),
			)

		order = OrderService().get_order(order_id)

		if not order:
			return result(
				"error",
				message=(f"{event.name}: could not load order {order_id} for payment {payment_id}"),
			)

		settings = frappe.get_doc(SETTING_DOCTYPE)
		sync = OrderSync(settings)
		so_name = sync.sync(order, request_id=event.log_name)

		if not so_name:
			return result(
				"invalid",
				message=(f"{event.name}: payment {payment_id} order {order_id} — SO not created"),
			)

		so = frappe.get_doc("Sales Order", so_name)
		si_name = InvoiceSync(settings).create(order, so)

		if not si_name:
			return result(
				"skipped",
				sales_order=so_name,
				message=(
					f"{event.name}: SO {so_name}; invoice skipped or already billed for payment {payment_id}"
				),
			)

		si = frappe.get_doc("Sales Invoice", si_name)

		PaymentSync(settings).reconcile(
			si,
			InvoiceSync.get_posting_date(order),
			payment_id=payment_id or None,
			order_id=order_id,
		)

		return result(
			"success",
			sales_order=so_name,
			sales_invoice=si_name,
			message=(
				f"{event.name}: Sales Invoice {si_name} for "
				f"{so_name} (payment {payment_id}, order {order_id})"
			),
		)


@register("order.return_requested", "order.return_received")
class ReturnHandler:
	def handle(self, event: MedusaEvent) -> dict:
		return result(
			"skipped",
			message=f"{event.name}: return {event.entity_id} (sync pending)",
		)
