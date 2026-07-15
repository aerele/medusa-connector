# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Order-domain webhook handlers → ERPNext Sales Order sync."""

from __future__ import annotations

import frappe
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


@register(
	"order.fulfillment_created",
	"order.fulfillment_canceled",
	# Aliases used in older docs / webhook plugin configs
	"fulfillment.canceled",
)
class FulfillmentHandler(BaseHandler):
	"""Medusa fulfillment → Delivery Note (create / cancel).

	Payload (Medusa v2)::

	    {"order_id": "order_…", "fulfillment_id": "fu_…"}

	Does not use ``resource="orders"`` because ``entity_id`` is not the order id
	(``id`` is absent; ids are ``order_id`` / ``fulfillment_id``).
	"""

	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		from medusa_connector.order.fulfillment import (
			cancel_fulfillment_delivery_note,
			sync_fulfillment,
		)

		entity = entity if isinstance(entity, dict) else {}
		order_id = cstr(entity.get("order_id") or "")
		fulfillment_id = cstr(entity.get("fulfillment_id") or entity.get("id") or event.entity_id or "")

		is_cancel = event.name in {"order.fulfillment_canceled", "fulfillment.canceled"}
		if is_cancel:
			result = cancel_fulfillment_delivery_note(
				fulfillment_id=fulfillment_id,
				order_id=order_id or None,
				request_id=event.log_name,
			)
		else:
			result = sync_fulfillment(
				order_id=order_id or None,
				fulfillment_id=fulfillment_id or None,
				request_id=event.log_name,
			)

		dn = result.get("delivery_note") or "-"
		so = result.get("sales_order") or "-"
		return (
			f"{event.name}: {result.get('status')} DN={dn} SO={so} "
			f"fulfillment={fulfillment_id or '-'} order={order_id or result.get('order_id') or '-'} "
			f"— {result.get('message') or ''}"
		).strip()


@register(
	"shipment.created",
	"delivery.created",
	# Alias from older connector docs
	"order.shipment_created",
)
class ShipmentHandler(BaseHandler):
	"""Shipment / delivery milestones on a fulfillment → ensure DN + tracking.

	Medusa v2 payloads::

	    shipment.created  → { "id": "<fulfillment_id>", ... }
	    delivery.created  → { "id": "<fulfillment_id>" }

	If the DN was already created on ``order.fulfillment_created``, this updates
	tracking/status. If not, it creates the DN when ``order_id`` can be resolved.
	"""

	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		from medusa_connector.order.fulfillment import update_fulfillment_tracking

		entity = entity if isinstance(entity, dict) else {}
		fulfillment_id = cstr(entity.get("id") or entity.get("fulfillment_id") or event.entity_id or "")
		order_id = cstr(entity.get("order_id") or "")

		# If only fulfillment id is present, try locating an existing DN's order id.
		if fulfillment_id and not order_id:
			from medusa_connector.constants import FULFILLMENT_ID_FIELD, ORDER_ID_FIELD

			order_id = cstr(
				frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, ORDER_ID_FIELD)
				or ""
			)

		if not fulfillment_id:
			return f"{event.name}: missing fulfillment id on payload"

		if not order_id:
			# Cannot load order/fulfillment without order_id (no GET /admin/fulfillments/:id).
			return (
				f"{event.name}: fulfillment {fulfillment_id} — no order_id and no existing DN; "
				"wait for order.fulfillment_created or include order_id"
			)

		result = update_fulfillment_tracking(
			fulfillment_id=fulfillment_id,
			order_id=order_id,
			request_id=event.log_name,
		)
		return (
			f"{event.name}: {result.get('status')} DN={result.get('delivery_note') or '-'} "
			f"fulfillment={fulfillment_id} order={order_id} — {result.get('message') or ''}"
		).strip()


@register("payment.captured", "payment.refunded")
class PaymentHandler(BaseHandler):
	"""On capture: resolve order from payment → ensure SO → optional SI + PE.

	Medusa v2 ``payment.*`` webhooks only include ``{"id": "pay_..."}``. The
	linked order is loaded via Admin ``GET /admin/payments/{id}`` expanding
	``payment_collection.order`` (see :class:`PaymentService`).
	"""

	resource = None  # not an /admin/orders/{id} resource; resolve via PaymentService

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		import frappe

		from medusa_connector.constants import SETTING_DOCTYPE
		from medusa_connector.medusa.exceptions import MedusaConnectorError
		from medusa_connector.medusa.payment import PaymentService
		from medusa_connector.order.invoice import create_sales_invoice

		entity = entity if isinstance(entity, dict) else {}
		payment_id = cstr(entity.get("id") or event.entity_id or "")

		try:
			payment_service = PaymentService()
			order_id = payment_service.resolve_order_id(entity, payment_id=payment_id)
		except MedusaConnectorError as exc:
			frappe.logger("medusa_connector").warning(
				f"{event.name}: failed to resolve order for payment {payment_id}: {exc}"
			)
			return f"{event.name}: could not resolve order for payment {payment_id} ({exc})"

		if not order_id:
			return (
				f"{event.name}: no order linked to payment {payment_id or '(unknown)'} "
				"(checked payload and Admin payment_collection.order)"
			)

		order = OrderService().get_order(order_id)
		if not order:
			return f"{event.name}: could not load order {order_id} for payment {payment_id}"

		so_name = sync_sales_order(order, request_id=event.log_name)
		if event.name == "payment.captured" and so_name:
			settings = frappe.get_doc(SETTING_DOCTYPE)
			so = frappe.get_doc("Sales Order", so_name)
			si_name = create_sales_invoice(order, settings, so, payment_id=payment_id or None)
			if si_name:
				return (
					f"{event.name}: Sales Invoice {si_name} for {so_name} "
					f"(payment {payment_id}, order {order_id})"
				)
			return (
				f"{event.name}: SO {so_name} (invoice skipped or already billed; "
				f"payment {payment_id}, order {order_id})"
			)
		if event.name == "payment.refunded":
			return f"{event.name}: payment {payment_id} order {order_id} (refund sync pending)"
		return f"{event.name}: payment {payment_id} order {order_id}"


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	"""Return events (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: return {event.entity_id} (sync pending)"
