# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin order API (list / get)."""

from __future__ import annotations

from medusa_connector.constants import DEFAULT_ORDER_FIELDS
from medusa_connector.medusa.client import MedusaClient


class OrderService:
	"""Fetch Medusa orders for webhook hydration and bulk sync."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_order(self, order_id: str, *, fields: str | None = DEFAULT_ORDER_FIELDS) -> dict:
		"""``GET /admin/orders/{id}`` — unwrap ``order`` key."""
		if not order_id:
			return {}
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest("GET", f"/admin/orders/{order_id}", params=params)
		return response.get("order", response) if isinstance(response, dict) else {}

	def list_orders(
		self,
		*,
		limit: int = 50,
		offset: int = 0,
		q: str | None = None,
		created_at_gt: str | None = None,
		created_at_lt: str | None = None,
		fields: str | None = DEFAULT_ORDER_FIELDS,
	) -> dict:
		"""``GET /admin/orders`` — paginated list."""
		params: dict = {"limit": limit, "offset": offset}
		if fields:
			params["fields"] = fields
		if q:
			params["q"] = q
		if created_at_gt:
			params["created_at[gt]"] = created_at_gt
		if created_at_lt:
			params["created_at[lt]"] = created_at_lt
		response = self.client.execute_rest("GET", "/admin/orders", params=params)
		if not isinstance(response, dict):
			return {"orders": [], "count": 0, "limit": limit, "offset": offset}
		return {
			"orders": response.get("orders") or [],
			"count": response.get("count", 0),
			"limit": response.get("limit", limit),
			"offset": response.get("offset", offset),
		}

	def iter_orders(
		self,
		*,
		page_size: int = 50,
		created_at_gt: str | None = None,
		created_at_lt: str | None = None,
		q: str | None = None,
	):
		"""Yield every order page-by-page (for Sync Old Orders)."""
		offset = 0
		while True:
			page = self.list_orders(
				limit=page_size,
				offset=offset,
				created_at_gt=created_at_gt,
				created_at_lt=created_at_lt,
				q=q,
			)
			orders = page.get("orders") or []
			if not orders:
				break
			yield from orders
			offset += len(orders)
			count = page.get("count")
			if count is not None and offset >= count:
				break
			if len(orders) < page_size:
				break
