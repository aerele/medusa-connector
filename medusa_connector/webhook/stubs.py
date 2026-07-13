# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Stub handlers for catalog / master-data events not yet mapped to ERPNext.

Kept in one module so registration stays complete for webhook sync without a
file-per-resource layer of empty services and mappers.
"""

from __future__ import annotations

from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


class _PendingHandler(BaseHandler):
	"""Acknowledge an event until domain sync is implemented."""

	label = "resource"

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		return f"{event.name}: {self.label} {event.entity_id} (sync pending)"


@register("customer.created", "customer.updated", "customer.deleted")
class CustomerHandler(_PendingHandler):
	label = "customer"


# inventory-item.* is handled by product.inventory_webhook (real sync).


@register("price-list.created", "price-list.updated", "price-list.deleted")
class PriceHandler(_PendingHandler):
	label = "price"


@register("product-category.created", "product-category.updated", "product-category.deleted")
class ProductCategoryHandler(_PendingHandler):
	label = "product category"


@register("product-collection.created", "product-collection.updated", "product-collection.deleted")
class ProductCollectionHandler(_PendingHandler):
	label = "product collection"


@register("region.created", "region.updated", "region.deleted")
class RegionHandler(_PendingHandler):
	label = "region"


@register("sales-channel.created", "sales-channel.updated", "sales-channel.deleted")
class SalesChannelHandler(_PendingHandler):
	label = "sales channel"


@register("stock-location.created", "stock-location.updated", "stock-location.deleted")
class StockLocationHandler(_PendingHandler):
	label = "stock location"
