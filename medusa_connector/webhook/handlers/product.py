# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe

from medusa_connector.mapper.product_mapper import ProductMapper
from medusa_connector.medusa.product import ProductService
from medusa_connector.sync.product_sync import ProductSync
from medusa_connector.utils.sync_guard import (
	should_skip_inbound_for_product,
	try_claim_product_sync,
)
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register(
	"product.created",
	"product.updated",
	"product.deleted",
)
class ProductHandler(BaseHandler):
	"""Hydrate, map, and synchronise Medusa products to ERPNext Items."""

	def __init__(
		self,
		service: ProductService | None = None,
		mapper: ProductMapper | None = None,
		sync: ProductSync | None = None,
	) -> None:
		self.service = service or ProductService()
		self.mapper = mapper or ProductMapper()
		self.sync_service = sync or ProductSync()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		product_id = event.entity_id

		if event.name == "product.deleted":
			if not product_id:
				return "Deleted product without id"
			item_code = self.sync_service.disable_product(product_id)
			return f"Deleted {product_id} → disabled {item_code or 'no mapping'}"

		if not product_id:
			raise ValueError("Product webhook does not contain a product id")

		# Echo of our own ERPNext → Medusa export: skip to break the loop.
		if should_skip_inbound_for_product(product_id):
			return f"{event.name}: skipped (outbound export echo for {product_id})"

		# Coalesce product.updated storms with concurrent variant events.
		if event.name == "product.updated" and not try_claim_product_sync(product_id):
			return f"{event.name}: skipped (already syncing {product_id})"

		product = self.service.get_product(product_id)
		mapped_product = self.mapper.map(product)
		result = self.sync_service.sync(mapped_product, force=True)
		return f"{event.name}: {result['action']} {result['item_code']}"
