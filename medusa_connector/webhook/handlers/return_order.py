# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	"""Reflect Medusa return events on the ERPNext order.

	Extension point: create a Sales Return / Credit Note against the linked Sales
	Order. Idempotent on the return id.
	"""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: return {event.entity_id}"
