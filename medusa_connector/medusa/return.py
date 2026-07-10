"""Medusa return service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class ReturnService:
	"""Access return data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_return(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Return fetching has not been implemented")
