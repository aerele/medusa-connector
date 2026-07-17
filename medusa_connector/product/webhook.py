# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Product-domain webhook handlers (Medusa → ERPNext)."""

from __future__ import annotations

from medusa_connector.medusa.product import ProductService
from medusa_connector.product.mapper import ProductMapper
from medusa_connector.product.sync import ProductSync
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


def _fetch_map_and_sync(
	product_id: str,
	service: ProductService | None = None,
	mapper: ProductMapper | None = None,
	sync: ProductSync | None = None,
	product: dict | None = None,
) -> tuple[dict, dict]:
	"""Fetch, map, and synchronise a complete Medusa product."""

	service = service or ProductService()
	mapper = mapper or ProductMapper()
	sync = sync or ProductSync()

	product = product or service.get_product(product_id)

	if not product:
		raise ValueError(f"Medusa product {product_id} was not found")

	mapped = mapper.map(product)
	result = sync.sync(mapped)

	return result, mapped


def _sync_message(
	event_name: str,
	result: dict,
	mapped: dict,
	product_id: str | None = None,
) -> str:
	message = (
		f"{event_name}: {result['action']} {result['item_code']} "
		f"(status={mapped.get('raw_status')} "
		f"disabled={mapped.get('disabled')})"
	)

	if product_id:
		message = (
			f"{event_name}: {result['action']} {result['item_code']} "
			f"(via product {product_id}; "
			f"status={mapped.get('raw_status')} "
			f"disabled={mapped.get('disabled')})"
		)

	return message


class ProductBaseHandler(BaseHandler):
	"""Shared product sync behaviour for product-related webhooks."""

	def __init__(
		self,
		service: ProductService | None = None,
		mapper: ProductMapper | None = None,
		sync: ProductSync | None = None,
	) -> None:
		self.service = service or ProductService()
		self.mapper = mapper or ProductMapper()
		self.sync_service = sync or ProductSync()

	def sync_product(
		self,
		event_name: str,
		product_id: str,
		*,
		via_variant: str | None = None,
		product: dict | None = None,
	) -> str:
		if not product_id:
			raise ValueError(f"{event_name}: product id is required")

		result, mapped = _fetch_map_and_sync(
			product_id,
			self.service,
			self.mapper,
			self.sync_service,
			product=product,
		)

		return _sync_message(
			event_name,
			result,
			mapped,
			product_id if via_variant else None,
		)


@register("product.created", "product.updated")
class ProductHandler(ProductBaseHandler):
	"""Synchronise a complete Medusa product into ERPNext."""

	def process(
		self,
		event: MedusaEvent,
		entity: dict,
	) -> str | None:
		product_id = event.entity_id

		if not product_id:
			raise ValueError("Product webhook does not contain a product id")

		return self.sync_product(
			event.name,
			product_id,
		)


@register("product-variant.created", "product-variant.updated")
class ProductVariantHandler(ProductBaseHandler):
	"""Synchronise the parent product when a variant changes."""

	def process(
		self,
		event: MedusaEvent,
		entity: dict,
	) -> str | None:
		variant_id = event.entity_id

		if not variant_id:
			raise ValueError("Product variant webhook does not contain a variant id")

		product, variant = self.service.get_product_by_variant_id(variant_id)

		if not variant:
			raise ValueError(f"Medusa variant {variant_id} was not found")

		product_id = (
			product.get("id") or variant.get("product_id") or (variant.get("product") or {}).get("id")
		)

		if not product_id:
			raise ValueError(f"Cannot resolve parent product for Medusa variant {variant_id}")

		return self.sync_product(
			event.name,
			product_id,
			via_variant=variant_id,
			product=product or None,
		)
