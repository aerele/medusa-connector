"""Webhook handler placeholder for Medusa product variant events."""

from medusa_connector.medusa.product_variant import ProductVariantService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("product-variant.created", "product-variant.updated", "product-variant.deleted")
class ProductVariantHandler(BaseHandler):
	"""Accept supported product variant events until sync is implemented."""

	def __init__(self, service: ProductVariantService | None = None) -> None:
		self.service = service or ProductVariantService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: product variant {event.entity_id} (sync pending)"
