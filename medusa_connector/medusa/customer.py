# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Medusa Admin customer API (used to hydrate order payloads).

Customer *creation* in ERPNext is not done here — see
``medusa_connector.customer.sync`` (Ecommerce Core, order-time only).
"""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient

# Expand addresses for ERPNext Address upsert. Bare field lists replace defaults.
DEFAULT_CUSTOMER_FIELDS = "*addresses,+metadata"


class CustomerService:
	"""Fetch Medusa customers for order-time hydration."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_customer(self, customer_id: str, *, fields: str | None = DEFAULT_CUSTOMER_FIELDS) -> dict:
		"""Fetch and unwrap one customer (with addresses when available)."""
		if not customer_id:
			return {}
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest("GET", f"/admin/customers/{customer_id}", params=params)
		return response.get("customer", response) if isinstance(response, dict) else {}
