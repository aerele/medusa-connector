import frappe

from medusa_connector.mapper.product_mapper import ProductMapper
from medusa_connector.medusa.product import ProductService
from medusa_connector.sync.product_sync import ProductSync
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register(
	"product.created",
	"product.updated",
	"product.deleted",
)
class ProductHandler(BaseHandler):
	"""Hydrate, map, and synchronise Medusa products to ERPNext Items."""

	def __init__(
		self,
		service: ProductService | None = None,
		mapper: ProductMapper | None = None,
		sync: ProductSync | None = None,
	) -> None:
		self.service = service or ProductService()
		self.mapper = mapper or ProductMapper()
		self.sync_service = sync or ProductSync()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		product_id = event.entity_id

		# Deleted products no longer exist in Medusa
		if event.name == "product.deleted":
			if product_id and frappe.db.exists("Item", product_id):
				item = frappe.get_doc("Item", product_id)
				item.disabled = 1
				item.save()
			return f"Deleted {product_id}"

		if not product_id:
			raise ValueError("Product webhook does not contain a product id")
		product = self.service.get_product(product_id)
		mapped_product = self.mapper.map(product)
		item_name = self.sync_service.sync(mapped_product)
		return f"{event.name}: {item_name}"
