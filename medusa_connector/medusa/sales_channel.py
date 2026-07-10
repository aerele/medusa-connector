"""Medusa sales channel service placeholder."""

from medusa_connector.medusa.client import MedusaClient


class SalesChannelService:
	"""Access sales channel data through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client

	def get_sales_channel(self, resource_id: str) -> dict:
		"""TODO: Fetch this resource from Medusa."""
		raise NotImplementedError("SalesChannel fetching has not been implemented")
