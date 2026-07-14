# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Order-domain webhook handlers → ERPNext Sales Order sync."""

from __future__ import annotations

from frappe.utils import cstr

from medusa_connector.medusa.order import OrderService
from medusa_connector.order.sync import cancel_order, get_sales_order, sync_sales_order
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


def _full_order(entity: dict, event: MedusaEvent) -> dict:
	"""Prefer hydrated Admin order; re-fetch when payload is thin."""
	order = entity if isinstance(entity, dict) else {}
	order_id = cstr(order.get("id") or event.entity_id or "")
	# Thin webhook body often only has id — always re-fetch for SO create.
	if order_id and (not order.get("items") or not order.get("customer_id")):
		fetched = OrderService().get_order(order_id)
		if fetched:
			return fetched
	return order


@register("order.placed")
class OrderPlacedHandler(BaseHandler):
	"""Create Sales Order (customer + items + taxes) on new Medusa order."""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		order = _full_order(entity, event)
		order_id = cstr(order.get("id") or event.entity_id or "")
		so_name = sync_sales_order(order, request_id=event.log_name)
		display = order.get("display_id") or order_id
		if so_name:
			return f"{event.name}: Sales Order {so_name} for order #{display}"
		return f"{event.name}: order #{display} not created (see Ecommerce Integration Log)"


@register("order.updated", "order.completed")
class OrderUpdatedHandler(BaseHandler):
	"""Create SO if missing; otherwise refresh status custom fields."""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		order = _full_order(entity, event)
		order_id = cstr(order.get("id") or event.entity_id or "")
		existing = get_sales_order(order_id) if order_id else None
		if existing:
			from medusa_connector.order.sync import update_order_status_fields

			update_order_status_fields(order)
			return f"{event.name}: updated status on {existing.name}"
		so_name = sync_sales_order(order, request_id=event.log_name)
		display = order.get("display_id") or order_id
		if so_name:
			return f"{event.name}: Sales Order {so_name} for order #{display}"
		return f"{event.name}: order #{display} — no SO created"


@register("order.canceled")
class OrderCanceledHandler(BaseHandler):
	"""Cancel ERPNext Sales Order when Medusa cancels (if safe)."""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		order = _full_order(entity, event)
		so_name = cancel_order(order, request_id=event.log_name)
		display = order.get("display_id") or order.get("id") or event.entity_id
		if so_name:
			return f"{event.name}: handled Sales Order {so_name} for order #{display}"
		return f"{event.name}: no Sales Order for order #{display}"


@register("order.fulfillment_created", "fulfillment.canceled")
class FulfillmentHandler(BaseHandler):
	"""Fulfillment → Delivery Note (pending; status note only for now)."""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		order_id = cstr((entity or {}).get("order_id") or event.entity_id or "")
		so = get_sales_order(order_id) if order_id else None
		if so:
			from medusa_connector.order.sync import update_order_status_fields

			update_order_status_fields(entity if entity.get("id") else {"id": order_id, **(entity or {})})
			return f"{event.name}: noted on {so.name} (delivery note sync pending)"
		return f"{event.name}: fulfillment {event.entity_id} (no Sales Order yet)"


@register("order.shipment_created")
class ShipmentHandler(BaseHandler):
	"""Shipment / tracking (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: shipment {event.entity_id} (sync pending)"


@register("payment.captured", "payment.refunded")
class PaymentHandler(BaseHandler):
	"""On capture: ensure SO exists, then Sales Invoice if enabled."""

	resource = None  # payment payloads are not /admin/orders/{id}

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		import frappe

		from medusa_connector.constants import SETTING_DOCTYPE
		from medusa_connector.order.invoice import create_sales_invoice

		entity = entity or {}
		order_id = cstr(entity.get("order_id") or "")
		if not order_id:
			return f"{event.name}: missing order_id on payment {event.entity_id}"

		order = OrderService().get_order(order_id)
		if not order:
			return f"{event.name}: could not load order {order_id}"

		so_name = sync_sales_order(order, request_id=event.log_name)
		if event.name == "payment.captured" and so_name:
			settings = frappe.get_doc(SETTING_DOCTYPE)
			so = frappe.get_doc("Sales Order", so_name)
			si_name = create_sales_invoice(order, settings, so)
			if si_name:
				return f"{event.name}: Sales Invoice {si_name} for {so_name}"
			return f"{event.name}: SO {so_name} (invoice skipped or already billed)"
		return f"{event.name}: payment {event.entity_id} order {order_id}"


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	"""Return events (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: return {event.entity_id} (sync pending)"
