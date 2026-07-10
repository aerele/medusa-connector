"""Webhook handler placeholder for Medusa product collection events."""

from medusa_connector.medusa.product_collection import ProductCollectionService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("product-collection.created", "product-collection.updated", "product-collection.deleted")
class ProductCollectionHandler(BaseHandler):
	"""Accept supported product collection events until sync is implemented."""

	def __init__(self, service: ProductCollectionService | None = None) -> None:
		self.service = service or ProductCollectionService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: product collection {event.entity_id} (sync pending)"
