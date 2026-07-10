"""Medusa product variant service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class ProductVariantService:
	"""Access product variant data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_product_variant(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("ProductVariant fetching has not been implemented")
