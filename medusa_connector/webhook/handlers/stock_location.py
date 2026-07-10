"""Webhook handler placeholder for Medusa stock location events."""

from medusa_connector.medusa.stock_location import StockLocationService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("stock-location.created", "stock-location.updated", "stock-location.deleted")
class StockLocationHandler(BaseHandler):
	"""Accept supported stock location events until sync is implemented."""

	def __init__(self, service: StockLocationService | None = None) -> None:
		self.service = service or StockLocationService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: stock location {event.entity_id} (sync pending)"
