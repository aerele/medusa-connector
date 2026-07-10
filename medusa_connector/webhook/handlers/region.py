"""Webhook handler placeholder for Medusa region events."""

from medusa_connector.medusa.region import RegionService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("region.created", "region.updated", "region.deleted")
class RegionHandler(BaseHandler):
	"""Accept supported region events until sync is implemented."""

	def __init__(self, service: RegionService | None = None) -> None:
		self.service = service or RegionService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: region {event.entity_id} (sync pending)"
