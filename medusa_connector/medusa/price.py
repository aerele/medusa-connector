"""Medusa price service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class PriceService:
	"""Access price data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_price(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Price fetching has not been implemented")
