"""Webhook handler placeholder for Medusa sales channel events."""

from medusa_connector.medusa.sales_channel import SalesChannelService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("sales-channel.created", "sales-channel.updated", "sales-channel.deleted")
class SalesChannelHandler(BaseHandler):
	"""Accept supported sales channel events until sync is implemented."""

	def __init__(self, service: SalesChannelService | None = None) -> None:
		self.service = service or SalesChannelService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: sales channel {event.entity_id} (sync pending)"
