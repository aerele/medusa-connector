# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Order-domain webhook handlers (stubs until Sales Order sync is wired).

Events are registered so webhook sync keeps Medusa subscriptions current.
Handlers acknowledge deliveries without mutating ERPNext yet.
"""

from __future__ import annotations

from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.placed", "order.updated", "order.canceled", "order.completed")
class OrderHandler(BaseHandler):
	"""Reconcile Medusa order lifecycle events against ERPNext (pending)."""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		display_id = entity.get("display_id") or event.entity_id
		status = entity.get("status") or entity.get("payment_status")
		return f"{event.name}: order #{display_id} (status={status})"


@register("order.fulfillment_created", "fulfillment.canceled")
class FulfillmentHandler(BaseHandler):
	"""Reflect Medusa fulfillment events on the ERPNext order (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: fulfillment {event.entity_id}"


@register("order.shipment_created")
class ShipmentHandler(BaseHandler):
	"""Reflect Medusa shipment / tracking events (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: shipment {event.entity_id}"


@register("payment.captured", "payment.refunded")
class PaymentHandler(BaseHandler):
	"""Record Medusa payment captures/refunds (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		amount = entity.get("amount")
		return f"{event.name}: payment {event.entity_id} (amount={amount})"


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	"""Reflect Medusa return events on the ERPNext order (pending)."""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: return {event.entity_id}"
