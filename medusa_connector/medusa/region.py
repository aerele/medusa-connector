"""Medusa region service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class RegionService:
	"""Access region data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_region(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Region fetching has not been implemented")
