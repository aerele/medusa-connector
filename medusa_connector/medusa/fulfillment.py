"""Medusa fulfillment service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class FulfillmentService:
	"""Access fulfillment data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_fulfillment(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Fulfillment fetching has not been implemented")
