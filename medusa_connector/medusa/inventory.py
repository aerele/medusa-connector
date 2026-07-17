# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa Admin inventory service.

Endpoints (Admin API):
- GET/POST ``/admin/inventory-items``
- GET/POST ``/admin/inventory-items/{id}``
- GET/POST ``/admin/inventory-items/{id}/location-levels``
- POST ``/admin/inventory-items/{id}/location-levels/{location_id}``
"""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient

# Expand product variants linked to an inventory item (Admin Inventory Item model).
DEFAULT_INVENTORY_ITEM_FIELDS = (
	"*variants,*location_levels,+hs_code,+mid_code,+origin_country,+material,"
	"+weight,+length,+height,+width,+sku,+title,+thumbnail,+metadata"
)


class InventoryService:
	"""Access inventory items and location levels through the shared Medusa client."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_inventory_item(
		self, inventory_item_id: str, *, fields: str | None = DEFAULT_INVENTORY_ITEM_FIELDS
	) -> dict:
		"""``GET /admin/inventory-items/{id}`` with optional relation expand."""
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest(
			"GET", f"/admin/inventory-items/{inventory_item_id}", params=params
		)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def list_inventory_items(self, *, limit: int = 50, offset: int = 0, q: str | None = None) -> dict:
		params: dict = {"limit": limit, "offset": offset}
		if q:
			params["q"] = q
		response = self.client.execute_rest("GET", "/admin/inventory-items", params=params)
		return response if isinstance(response, dict) else {"inventory_items": []}

	def create_inventory_item(self, payload: dict) -> dict:
		"""``POST /admin/inventory-items`` — AdminCreateInventoryItem."""
		response = self.client.execute_rest("POST", "/admin/inventory-items", json=payload)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def update_inventory_item(self, inventory_item_id: str, payload: dict) -> dict:
		response = self.client.execute_rest(
			"POST", f"/admin/inventory-items/{inventory_item_id}", json=payload
		)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def list_location_levels(self, inventory_item_id: str) -> list[dict]:
		response = self.client.execute_rest(
			"GET", f"/admin/inventory-items/{inventory_item_id}/location-levels"
		)
		if not isinstance(response, dict):
			return []
		return response.get("inventory_levels") or response.get("location_levels") or []

	def create_location_level(
		self,
		inventory_item_id: str,
		*,
		location_id: str,
		stocked_quantity: float | int = 0,
		incoming_quantity: float | int = 0,
	) -> dict:
		"""``POST /admin/inventory-items/{id}/location-levels``.

		Body requires ``location_id`` (AdminBatchCreateInventoryItemLocationLevels).
		"""
		payload = {
			"location_id": location_id,
			"stocked_quantity": stocked_quantity,
			"incoming_quantity": incoming_quantity,
		}
		response = self.client.execute_rest(
			"POST",
			f"/admin/inventory-items/{inventory_item_id}/location-levels",
			json=payload,
		)
		return response if isinstance(response, dict) else {}

	def update_location_level(
		self,
		inventory_item_id: str,
		location_id: str,
		*,
		stocked_quantity: float | int | None = None,
		incoming_quantity: float | int | None = None,
	) -> dict:
		"""``POST /admin/inventory-items/{id}/location-levels/{location_id}``.

		Body: AdminUpdateInventoryLevel (``stocked_quantity``, ``incoming_quantity``).
		"""
		payload: dict = {}
		if stocked_quantity is not None:
			payload["stocked_quantity"] = stocked_quantity
		if incoming_quantity is not None:
			payload["incoming_quantity"] = incoming_quantity
		response = self.client.execute_rest(
			"POST",
			f"/admin/inventory-items/{inventory_item_id}/location-levels/{location_id}",
			json=payload,
		)
		return response if isinstance(response, dict) else {}

	def set_stocked_quantity(
		self,
		inventory_item_id: str,
		location_id: str,
		stocked_quantity: float | int,
	) -> dict:
		"""Upsert a location level quantity (create if missing, else update)."""
		levels = self.list_location_levels(inventory_item_id)
		exists = any(
			(level.get("location_id") == location_id or level.get("stock_location_id") == location_id)
			for level in levels
		)
		if exists:
			return self.update_location_level(
				inventory_item_id, location_id, stocked_quantity=stocked_quantity
			)
		return self.create_location_level(
			inventory_item_id, location_id=location_id, stocked_quantity=stocked_quantity
		)
