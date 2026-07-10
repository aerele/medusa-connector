"""Webhook handler placeholder for Medusa inventory events."""

from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("inventory-item.created", "inventory-item.updated", "inventory-item.deleted")
class InventoryHandler(BaseHandler):
	"""Accept supported inventory events until sync is implemented."""

	def __init__(self, service: InventoryService | None = None) -> None:
		self.service = service or InventoryService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: inventory {event.entity_id} (sync pending)"
