# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.medusa.fulfillment import FulfillmentService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.fulfillment_created", "fulfillment.canceled")
class FulfillmentHandler(BaseHandler):
	"""Reflect Medusa fulfillment events on the ERPNext order.

	Extension point: advance the linked Sales Order / create a Delivery Note when
	items are fulfilled. Idempotent on the fulfillment id.
	"""

	def __init__(self, service: FulfillmentService | None = None) -> None:
		self.service = service or FulfillmentService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: fulfillment {event.entity_id}"
