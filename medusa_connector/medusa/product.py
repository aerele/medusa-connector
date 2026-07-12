# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin product service (list / get / create / update / delete / variants).

Endpoints (Admin API):
- GET/POST ``/admin/products``
- GET/POST/DELETE ``/admin/products/{id}``
- GET/POST ``/admin/products/{id}/variants``
- POST/DELETE ``/admin/products/{id}/variants/{variant_id}``
- POST ``/admin/products/{id}/variants/batch``
"""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient

# Expand relations for import mapping. Use `*relation` form only: bare field lists
# replace default product fields and drop title/status. See Admin API
# "Select Fields and Relations".
DEFAULT_PRODUCT_FIELDS = "*variants,*options,*images,*categories,*tags,*collection,*sales_channels,+metadata"


class ProductService:
	"""Talk to Medusa Admin product endpoints through the shared client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_product(self, product_id: str, *, fields: str | None = DEFAULT_PRODUCT_FIELDS) -> dict:
		"""Fetch and unwrap one full product."""
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest("GET", f"/admin/products/{product_id}", params=params)
		return response.get("product", response) if isinstance(response, dict) else {}

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
		"""Return a page of products plus pagination metadata.

		Response shape: ``{products: [...], count, limit, offset}``.
		"""
		params: dict = {"limit": limit, "offset": offset}
		if q:
			params["q"] = q
		if status:
			params["status[]"] = status
		if updated_at_gt:
			params["updated_at[gt]"] = updated_at_gt
		if fields:
			params["fields"] = fields
		response = self.client.execute_rest("GET", "/admin/products", params=params)
		if not isinstance(response, dict):
			return {"products": [], "count": 0, "limit": limit, "offset": offset}
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
	):
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
		"""``POST /admin/products`` — body: AdminCreateProduct (+ additional_data)."""
		response = self.client.execute_rest("POST", "/admin/products", json=payload)
		return response.get("product", response) if isinstance(response, dict) else {}

	def update_product(self, product_id: str, payload: dict) -> dict:
		"""``POST /admin/products/{id}`` — body: AdminUpdateProduct."""
		response = self.client.execute_rest("POST", f"/admin/products/{product_id}", json=payload)
		return response.get("product", response) if isinstance(response, dict) else {}

	def delete_product(self, product_id: str) -> dict:
		"""``DELETE /admin/products/{id}``."""
		response = self.client.execute_rest("DELETE", f"/admin/products/{product_id}")
		return response if isinstance(response, dict) else {}

	def create_variant(self, product_id: str, payload: dict) -> dict:
		"""``POST /admin/products/{id}/variants`` — body: AdminCreateProductVariant."""
		response = self.client.execute_rest("POST", f"/admin/products/{product_id}/variants", json=payload)
		return response.get("product", response) if isinstance(response, dict) else {}

	def update_variant(self, product_id: str, variant_id: str, payload: dict) -> dict:
		"""``POST /admin/products/{id}/variants/{variant_id}`` — AdminUpdateProductVariant."""
		response = self.client.execute_rest(
			"POST",
			f"/admin/products/{product_id}/variants/{variant_id}",
			json=payload,
		)
		return response.get("product", response) if isinstance(response, dict) else {}

	def delete_variant(self, product_id: str, variant_id: str) -> dict:
		"""``DELETE /admin/products/{id}/variants/{variant_id}``."""
		response = self.client.execute_rest("DELETE", f"/admin/products/{product_id}/variants/{variant_id}")
		return response if isinstance(response, dict) else {}

	def batch_variants(
		self,
		product_id: str,
		*,
		create: list[dict] | None = None,
		update: list[dict] | None = None,
		delete: list[str] | None = None,
	) -> dict:
		"""``POST /admin/products/{id}/variants/batch`` — AdminBatchProductVariantRequest."""
		payload: dict = {}
		if create:
			payload["create"] = create
		if update:
			payload["update"] = update
		if delete:
			payload["delete"] = delete
		response = self.client.execute_rest(
			"POST", f"/admin/products/{product_id}/variants/batch", json=payload
		)
		return response if isinstance(response, dict) else {}
