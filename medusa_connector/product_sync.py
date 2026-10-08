# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Product Sync adapter for the shared Ecommerce Core page.

Registers this connector with the ``ecommerce_product_sync_providers`` hook
(see hooks.py) so the shared Product Sync Desk page can list Medusa products
and drive syncs. This is a thin facade over the existing product services and
the sync logic used by the connector's own page — no new sync behavior.

Single sync deliberately uses ``_sync_one`` without per-product log entries:
the shared page records one Ecommerce Integration Log per bulk run and logs
single-sync failures itself.
"""

from __future__ import annotations

import frappe

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.medusa.product import ProductService
from medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products import (
	_sync_one,
	list_medusa_products,
)


class MedusaProductSyncAdapter:
	"""Contract implementation for ``ecommerce_core.product_sync``."""

	def is_enabled(self) -> bool:
		return bool(frappe.db.get_single_value(SETTING_DOCTYPE, "enabled"))

	def get_products(self, limit: int, cursor: str | None = None, search: str | None = None) -> dict:
		offset = _decode_offset_cursor(cursor)
		result = list_medusa_products(offset=offset, limit=limit, q=search)

		total = int(result.get("count") or 0)
		return {
			"products": [
				{
					"id": product.get("id"),
					"name": product.get("title"),
					"sku": product.get("sku"),
					"variants": product.get("variants") or [],
				}
				for product in result.get("products") or []
			],
			"total": total,
			"next_cursor": f"o:{offset + limit}" if offset + limit < total else None,
		}

	def get_provider_count(self) -> int:
		page = ProductService().list_products(limit=1, offset=0)
		return int(page.get("count") or 0)

	def sync_product(self, product_id: str) -> dict:
		result = _sync_one(product_id, force=True)
		return {"action": result["action"], "item_code": result["item_code"]}


def _decode_offset_cursor(cursor: str | None) -> int:
	"""Opaque ``o:<offset>`` tokens, the same convention Ecommerce Core uses."""
	if not cursor or not str(cursor).startswith("o:"):
		return 0
	try:
		return max(int(str(cursor)[2:]), 0)
	except ValueError:
		return 0
