# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin product-variant helpers (list/get via product or variants index)."""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient
from medusa_connector.medusa.product import ProductService


class ProductVariantService:
	"""Thin wrapper around product-scoped variant Admin API routes."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()
		self.products = ProductService(self.client)

	def get_product_variant(self, product_id: str, variant_id: str) -> dict:
		"""``GET /admin/products/{id}/variants/{variant_id}`` when available via product."""
		product = self.products.get_product(product_id)
		for variant in product.get("variants") or []:
			if variant.get("id") == variant_id:
				return variant
		# Fallback list endpoint (Admin product-variants index).
		response = self.client.execute_rest(
			"GET",
			"/admin/product-variants",
			params={"id[]": variant_id, "limit": 1},
		)
		variants = (response or {}).get("variants") or []
		return variants[0] if variants else {}

	def create(self, product_id: str, payload: dict) -> dict:
		return self.products.create_variant(product_id, payload)

	def update(self, product_id: str, variant_id: str, payload: dict) -> dict:
		return self.products.update_variant(product_id, variant_id, payload)

	def delete(self, product_id: str, variant_id: str) -> dict:
		return self.products.delete_variant(product_id, variant_id)
