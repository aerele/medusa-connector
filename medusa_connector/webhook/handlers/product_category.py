"""Webhook handler placeholder for Medusa product category events."""

from medusa_connector.medusa.product_category import ProductCategoryService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("product-category.created", "product-category.updated", "product-category.deleted")
class ProductCategoryHandler(BaseHandler):
	"""Accept supported product category events until sync is implemented."""

	def __init__(self, service: ProductCategoryService | None = None) -> None:
		self.service = service or ProductCategoryService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: product category {event.entity_id} (sync pending)"
