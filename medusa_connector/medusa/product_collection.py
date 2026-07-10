"""Medusa product collection service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class ProductCollectionService:
	"""Access product collection data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_product_collection(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("ProductCollection fetching has not been implemented")
