"""Medusa order service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class OrderService:
	"""Access order data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_order(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Order fetching has not been implemented")
