# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("payment.captured", "payment.refunded")
class PaymentHandler(BaseHandler):
	"""Record Medusa payment captures/refunds against the ERPNext order.

	Extension point: create/update a Payment Entry linked to the matching Sales
	Order. Idempotency is anchored on the payment id carried in the payload.
	"""

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		amount = entity.get("amount")
		return f"{event.name}: payment {event.entity_id} (amount={amount})"
