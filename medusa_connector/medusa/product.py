# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin product service (list / get / create / update / delete / variants).

Endpoints (Admin API):
- GET/POST ``/admin/products``
- GET/POST/DELETE ``/admin/products/{id}``
- GET/POST ``/admin/products/{id}/variants``
- POST/DELETE ``/admin/products/{id}/variants/{variant_id}``
"""

from __future__ import annotations

from collections.abc import Iterator

from medusa_connector.medusa.client import MedusaClient

PRODUCTS_ENDPOINT = "/admin/products"
PRODUCT_VARIANTS_ENDPOINT = "/admin/product-variants"


DEFAULT_PRODUCT_FIELDS = (
	"*variants,*variants.options,*variants.options.option,*variants.prices,"
	"*options,*options.values,*images,*categories,*categories.parent_category,"
	"*tags,*collection,*type,*sales_channels,+metadata"
)


class ProductService:
	"""Talk to Medusa Admin product endpoints through the shared client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def _unwrap_product(self, response: object) -> dict:
		"""Extract product object from Medusa API response."""
		return response.get("product", response)

	def _build_list_params(
		self,
		*,
		limit: int,
		offset: int,
		q: str | None = None,
		status: str | None = None,
		updated_at_gt: str | None = None,
		fields: str | None = None,
	) -> dict:
		"""Build product listing query parameters."""
		params = {
			"limit": limit,
			"offset": offset,
		}

		if q:
			params["q"] = q

		if status:
			params["status[]"] = status

		if updated_at_gt:
			params["updated_at[gt]"] = updated_at_gt

		if fields:
			params["fields"] = fields

		return params

	def get_product(
		self,
		product_id: str,
		fields: str | None = DEFAULT_PRODUCT_FIELDS,
	) -> dict:
		"""Fetch and unwrap one full product."""
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest(
			"GET",
			f"{PRODUCTS_ENDPOINT}/{product_id}",
			params=params,
		)

		return self._unwrap_product(response)

	def list_products(
		self,
		*,
		limit: int = 50,
		offset: int = 0,
		q: str | None = None,
		status: str | None = None,
		updated_at_gt: str | None = None,
		fields: str | None = None,
	) -> dict:
		"""Return a page of products plus pagination metadata."""

		params = self._build_list_params(
			limit=limit,
			offset=offset,
			q=q,
			status=status,
			updated_at_gt=updated_at_gt,
			fields=fields,
		)

		response = self.client.execute_rest(
			"GET",
			PRODUCTS_ENDPOINT,
			params=params,
		)

		products = response.get("products") or []

		return {
			"products": products,
			"count": response.get("count", len(products)),
			"limit": response.get("limit", limit),
			"offset": response.get("offset", offset),
		}

	def iter_products(
		self,
		*,
		page_size: int = 50,
		q: str | None = None,
		status: str | None = None,
		updated_at_gt: str | None = None,
		fields: str | None = None,
	) -> Iterator[dict]:
		"""Yield every product across all pages."""

		offset = 0

		while True:
			page = self.list_products(
				limit=page_size,
				offset=offset,
				q=q,
				status=status,
				updated_at_gt=updated_at_gt,
				fields=fields,
			)

			products = page.get("products") or []

			if not products:
				break

			yield from products

			offset += len(products)

			count = page.get("count")

			if count is not None and offset >= count:
				break

			if len(products) < page_size:
				break

	def create_product(self, payload: dict) -> dict:
		"""Create product."""
		response = self.client.execute_rest(
			"POST",
			PRODUCTS_ENDPOINT,
			json=payload,
		)

		return self._unwrap_product(response)

	def update_product(self, product_id: str, payload: dict) -> dict:
		"""Update product."""
		response = self.client.execute_rest(
			"POST",
			f"{PRODUCTS_ENDPOINT}/{product_id}",
			json=payload,
		)

		return self._unwrap_product(response)

	def create_variant(self, product_id: str, payload: dict) -> dict:
		"""Create product variant."""
		response = self.client.execute_rest(
			"POST",
			f"{PRODUCTS_ENDPOINT}/{product_id}/variants",
			json=payload,
		)

		return self._unwrap_product(response)

	def update_variant(
		self,
		product_id: str,
		variant_id: str,
		payload: dict,
	) -> dict:
		"""Update product variant."""
		response = self.client.execute_rest(
			"POST",
			f"{PRODUCTS_ENDPOINT}/{product_id}/variants/{variant_id}",
			json=payload,
		)

		return self._unwrap_product(response)

	def batch_variants(
		self,
		product_id: str,
		*,
		create: list[dict] | None = None,
		update: list[dict] | None = None,
		delete: list[str] | None = None,
	) -> dict:
		"""Batch create/update/delete variants."""

		payload = {}

		if create:
			payload["create"] = create

		if update:
			payload["update"] = update

		if delete:
			payload["delete"] = delete

		response = self.client.execute_rest(
			"POST",
			f"{PRODUCTS_ENDPOINT}/{product_id}/variants/batch",
			json=payload,
		)

		return response

	def _request_variant(
		self,
		params: dict,
		fields: str | None = None,
	) -> dict:
		"""Fetch a single variant from the Medusa Admin API."""
		if fields:
			params["fields"] = fields

		response = self.client.execute_rest(
			"GET",
			PRODUCT_VARIANTS_ENDPOINT,
			params=params,
		)

		variants = response.get("variants") or []

		for variant in variants:
			if isinstance(variant, dict):
				return variant

		return {}

	def get_variant(
		self,
		variant_id: str,
		*,
		fields: str | None = None,
	) -> dict:
		"""Fetch a Medusa product variant by its ID."""
		if not variant_id:
			return {}

		variant_fields = fields or (
			"*options,*options.option,*prices,*inventory_items,*inventory_items.inventory_item"
		)

		query_formats = (
			{"id": variant_id, "limit": 1},
			{"id[]": variant_id, "limit": 1},
		)

		for params in query_formats:
			variant = self._request_variant(
				dict(params),
				variant_fields,
			)

			if variant and variant.get("id") == variant_id:
				return variant

		return {}

	def get_product_variant(
		self,
		product_id: str,
		variant_id: str,
	) -> dict:
		"""Resolve a variant from a known parent product."""
		if not product_id or not variant_id:
			return {}

		product = self.get_product(product_id)

		for variant in product.get("variants") or []:
			if variant.get("id") == variant_id:
				return variant

		return {}

	def get_product_by_variant_id(
		self,
		variant_id: str,
	) -> tuple[dict, dict]:
		"""Fetch the complete parent product and variant for a variant ID.

		Returns:
			(product, variant)

		The variant webhook only provides the variant ID. This method resolves
		the parent product so the normal ProductSync flow can be used.
		"""
		if not variant_id:
			return {}, {}

		variant = self.get_variant(variant_id)

		if not variant:
			return {}, {}

		product_id = variant.get("product_id") or (variant.get("product") or {}).get("id")

		if not product_id:
			return {}, variant

		product = self.get_product(product_id)

		if not product:
			return {}, variant

		return product, variant
