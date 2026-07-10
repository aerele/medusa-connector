"""Medusa inventory service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class InventoryService:
	"""Access inventory data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_inventory(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Inventory fetching has not been implemented")
