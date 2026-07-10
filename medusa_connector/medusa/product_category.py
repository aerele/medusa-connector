"""Medusa product category service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class ProductCategoryService:
	"""Access product category data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_product_category(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("ProductCategory fetching has not been implemented")
