"""Medusa payment service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class PaymentService:
	"""Access payment data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_payment(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("Payment fetching has not been implemented")
