"""Medusa product service."""

from medusa_connector.medusa.client import MedusaClient


class ProductService:
	"""Fetch products through the shared authenticated Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_product(self, product_id: str) -> dict:
		"""Fetch and unwrap one full product from the Medusa Admin API."""
		response = self.client.execute_rest("GET", f"/admin/products/{product_id}")
		return response.get("product", response) if isinstance(response, dict) else {}
