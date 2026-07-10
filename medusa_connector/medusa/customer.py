"""Medusa customer service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class CustomerService:
	"""Access customer data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_customer(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Customer fetching has not been implemented")
