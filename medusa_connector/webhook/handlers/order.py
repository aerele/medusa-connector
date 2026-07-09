# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("order.placed", "order.updated", "order.canceled", "order.completed")
class OrderHandler(BaseHandler):
	"""Reconcile Medusa order lifecycle events against ERPNext.

	ERPNext is the source of truth, so these events keep the ERP order's Medusa
	mirror in sync (status transitions, cancellations). The full order is fetched
	from Medusa before applying, and matching is by the Medusa order id so repeat
	deliveries are idempotent.
	"""

	resource = "orders"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		display_id = entity.get("display_id") or event.entity_id
		status = entity.get("status") or entity.get("payment_status")
		# Extension point: map to a Sales Order (find-or-update by medusa order id).
		# Kept as a structured outcome by default so the framework is safe to run
		# before the ERP mapping is wired for a given deployment.
		return f"{event.name}: order #{display_id} (status={status})"
