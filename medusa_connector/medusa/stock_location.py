"""Medusa stock location service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class StockLocationService:
	"""Access stock location data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_stock_location(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("StockLocation fetching has not been implemented")
