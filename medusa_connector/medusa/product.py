# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin product service (list / get / create / update / delete)."""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient


class ProductService:
	"""Talk to Medusa Admin product endpoints through the shared client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_product(self, product_id: str) -> dict:
		"""Fetch and unwrap one full product."""
		response = self.client.execute_rest("GET", f"/admin/products/{product_id}")
		return response.get("product", response) if isinstance(response, dict) else {}

	def list_products(
		self,
		*,
		limit: int = 50,
		offset: int = 0,
		q: str | None = None,
		status: str | None = None,
		updated_at_gt: str | None = None,
	) -> dict:
		"""Return a page of products plus pagination metadata.

		Response shape: ``{products: [...], count, limit, offset}``.
		"""
		params: dict = {"limit": limit, "offset": offset}
		if q:
			params["q"] = q
		# Medusa v2 admin filters commonly accept status[] / updated_at[gt].
		if status:
			params["status[]"] = status
		if updated_at_gt:
			params["updated_at[gt]"] = updated_at_gt
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
		response = self.client.execute_rest("POST", "/admin/products", json=payload)
		return response.get("product", response) if isinstance(response, dict) else {}

	def update_product(self, product_id: str, payload: dict) -> dict:
		response = self.client.execute_rest("POST", f"/admin/products/{product_id}", json=payload)
		return response.get("product", response) if isinstance(response, dict) else {}

	def delete_product(self, product_id: str) -> dict:
		response = self.client.execute_rest("DELETE", f"/admin/products/{product_id}")
		return response if isinstance(response, dict) else {}
