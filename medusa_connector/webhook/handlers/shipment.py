# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.shipment_created")
class ShipmentHandler(BaseHandler):
	"""Reflect Medusa shipment events (tracking) on the ERPNext order.

	Extension point: submit the linked Delivery Note / store tracking numbers.
	Idempotent on the shipment/fulfillment id.
	"""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: shipment {event.entity_id}"
