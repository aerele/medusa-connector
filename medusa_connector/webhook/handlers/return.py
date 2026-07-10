"""Webhook handler placeholder for Medusa return events."""

from importlib import import_module

from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register

ReturnService = import_module("medusa_connector.medusa.return").ReturnService


@register("order.return_requested", "order.return_received")
class ReturnHandler(BaseHandler):
	"""Accept supported return events until sync is implemented."""

	def __init__(self, service: ReturnService | None = None) -> None:
		self.service = service or ReturnService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: return {event.entity_id} (sync pending)"
