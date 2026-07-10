"""Webhook handler placeholder for Medusa price events."""

from medusa_connector.medusa.price import PriceService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("price-list.created", "price-list.updated", "price-list.deleted")
class PriceHandler(BaseHandler):
	"""Accept supported price events until sync is implemented."""

	def __init__(self, service: PriceService | None = None) -> None:
		self.service = service or PriceService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: price {event.entity_id} (sync pending)"
