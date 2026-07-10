"""Webhook handler placeholder for Medusa customer events."""

from medusa_connector.medusa.customer import CustomerService
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("customer.created", "customer.updated", "customer.deleted")
class CustomerHandler(BaseHandler):
	"""Accept supported customer events until sync is implemented."""

	def __init__(self, service: CustomerService | None = None) -> None:
		self.service = service or CustomerService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: customer {event.entity_id} (sync pending)"
