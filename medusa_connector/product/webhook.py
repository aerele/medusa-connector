# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Product-domain webhook event registrations."""

from __future__ import annotations

from medusa_connector.webhook.dispatch import MedusaEvent
from medusa_connector.webhook.registry import register


@register("product.created", "product.updated")
class ProductHandler:
	"""Handle Medusa product events."""

	resource = "products"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: product {event.entity_id} handler registered",
		}


@register("product-variant.created", "product-variant.updated")
class ProductVariantHandler:
	"""Handle Medusa product variant events."""

	resource = "product-variants"

	def handle(self, event: MedusaEvent) -> dict:
		return {
			"status": "skipped",
			"message": f"{event.name}: product variant {event.entity_id} handler registered",
		}
