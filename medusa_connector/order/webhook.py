# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Order-domain webhook event registrations."""

from __future__ import annotations

from medusa_connector.webhook.dispatch import MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.placed")
class OrderPlacedHandler:
	"""Handle a newly placed Medusa order."""

	resource = "orders"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: order {event.entity_id} handler registered",
		}


@register("order.updated", "order.completed")
class OrderUpdatedHandler:
	"""Handle an updated or completed Medusa order."""

	resource = "orders"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: order {event.entity_id} handler registered",
		}


@register("order.canceled")
class OrderCanceledHandler:
	"""Handle a canceled Medusa order."""

	resource = "orders"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: order {event.entity_id} handler registered",
		}


@register("order.fulfillment_created", "order.fulfillment_canceled", "fulfillment.canceled")
class FulfillmentHandler:
	"""Handle Medusa fulfillment events."""

	resource = None

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: fulfillment handler registered",
		}


@register("shipment.created", "delivery.created", "order.shipment_created")
class ShipmentHandler:
	"""Handle Medusa shipment and delivery events."""

	resource = None

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: shipment handler registered",
		}


@register("payment.captured", "payment.refunded")
class PaymentHandler:
	"""Handle Medusa payment events."""

	resource = None

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: payment handler registered",
		}


@register("order.return_requested", "order.return_received")
class ReturnHandler:
	"""Handle Medusa return events."""

	resource = "orders"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: return handler registered",
		}
