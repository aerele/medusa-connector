# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Medusa Admin inventory service.

Endpoints (Admin API):
* GET/POST  `/admin/inventory-items`
* GET/POST  `/admin/inventory-items/{id}`
* GET/POST  `/admin/inventory-items/{id}/location-levels`
* POST      `/admin/inventory-items/{id}/location-levels/{location_id}`
* POST      `/admin/products/{product_id}/variants/{variant_id}/inventory-items`
"""

from __future__ import annotations

from medusa_connector.medusa.client import MedusaClient

DEFAULT_INVENTORY_ITEM_FIELDS = (
	"*variants,*location_levels,+hs_code,+mid_code,+origin_country,+material,"
	"+weight,+length,+height,+width,+sku,+title,+thumbnail,+metadata"
)

INVENTORY_ITEMS_ENDPOINT = "/admin/inventory-items"


class InventoryService:
	"""Access Medusa inventory items and inventory location levels."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_inventory_item(
		self,
		inventory_item_id: str,
		*,
		fields: str | None = DEFAULT_INVENTORY_ITEM_FIELDS,
	) -> dict:
		"""Get a single Medusa inventory item."""
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest(
			"GET",
			f"{INVENTORY_ITEMS_ENDPOINT}/{inventory_item_id}",
			params=params,
		)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def list_inventory_items(
		self,
		*,
		limit: int = 50,
		offset: int = 0,
		q: str | None = None,
	) -> dict:
		"""List Medusa inventory items."""
		params = {
			"limit": limit,
			"offset": offset,
		}
		if q:
			params["q"] = q
		response = self.client.execute_rest(
			"GET",
			INVENTORY_ITEMS_ENDPOINT,
			params=params,
		)
		return response if isinstance(response, dict) else {"inventory_items": []}

	def create_inventory_item(self, payload: dict) -> dict:
		"""Create a Medusa inventory item.

		NOTE: Medusa v2's ``POST /admin/inventory-items`` does NOT accept
		a ``variant_id`` field (that was the Medusa v1 API and will return
		a 400 Bad Request on v2). To link the created item to a variant,
		use :meth:`link_inventory_item_to_variant` afterwards.
		"""
		response = self.client.execute_rest(
			"POST",
			INVENTORY_ITEMS_ENDPOINT,
			json=payload,
		)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def update_inventory_item(
		self,
		inventory_item_id: str,
		payload: dict,
	) -> dict:
		"""Update an existing Medusa inventory item."""
		response = self.client.execute_rest(
			"POST",
			f"{INVENTORY_ITEMS_ENDPOINT}/{inventory_item_id}",
			json=payload,
		)
		return response.get("inventory_item", response) if isinstance(response, dict) else {}

	def link_inventory_item_to_variant(
		self,
		product_id: str,
		variant_id: str,
		inventory_item_id: str,
		*,
		required_quantity: int = 1,
	) -> dict:
		"""Link an existing inventory item to a product variant.

		Medusa v2 creates inventory items and variant links as two
		separate operations. This calls
		``POST /admin/products/{product_id}/variants/{variant_id}/inventory-items``,
		the same endpoint used by Medusa's inventory-kit feature to
		attach inventory items to a variant.
		"""
		payload = {
			"inventory_item_id": inventory_item_id,
			"required_quantity": required_quantity,
		}
		response = self.client.execute_rest(
			"POST",
			f"/admin/products/{product_id}/variants/{variant_id}/inventory-items",
			json=payload,
		)
		return response if isinstance(response, dict) else {}

	def list_location_levels(self, inventory_item_id: str) -> list[dict]:
		"""Get all location levels for an inventory item."""
		response = self.client.execute_rest(
			"GET",
			f"{INVENTORY_ITEMS_ENDPOINT}/{inventory_item_id}/location-levels",
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
		"""Create an inventory item location level."""
		payload = {
			"location_id": location_id,
			"stocked_quantity": stocked_quantity,
			"incoming_quantity": incoming_quantity,
		}
		response = self.client.execute_rest(
			"POST",
			f"{INVENTORY_ITEMS_ENDPOINT}/{inventory_item_id}/location-levels",
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
		"""Update an existing inventory item location level."""
		payload: dict = {}
		if stocked_quantity is not None:
			payload["stocked_quantity"] = stocked_quantity
		if incoming_quantity is not None:
			payload["incoming_quantity"] = incoming_quantity
		response = self.client.execute_rest(
			"POST",
			f"{INVENTORY_ITEMS_ENDPOINT}/{inventory_item_id}/location-levels/{location_id}",
			json=payload,
		)
		return response if isinstance(response, dict) else {}

	def set_stocked_quantity(
		self,
		inventory_item_id: str,
		location_id: str,
		stocked_quantity: float | int,
	) -> dict:
		"""Create or update the location-level stocked quantity."""
		levels = self.list_location_levels(inventory_item_id)
		existing_level = next(
			(
				level
				for level in levels
				if (level.get("location_id") == location_id or level.get("stock_location_id") == location_id)
			),
			None,
		)
		if existing_level:
			return self.update_location_level(
				inventory_item_id,
				location_id,
				stocked_quantity=stocked_quantity,
			)
		return self.create_location_level(
			inventory_item_id,
			location_id=location_id,
			stocked_quantity=stocked_quantity,
		)

	def ensure_inventory_item_for_variant(
		self,
		variant_id: str,
		*,
		variant: dict | None = None,
		product_title: str | None = None,
	) -> str:
		"""Ensure exactly one inventory item is linked to a variant.

		Behavior:
		1. Zero inventory items:
		   Create one, then explicitly link it to the variant via
		   :meth:`link_inventory_item_to_variant`.
		2. Exactly one inventory item:
		   Reuse the existing inventory item.
		3. Two or more inventory items:
		   Fail explicitly and never guess.
		"""
		variant = variant or {}
		inventory_items = self._get_variant_inventory_items(variant)
		inventory_item_count = len(inventory_items)

		# ---------------------------------------------------------
		# CASE 2:
		# Exactly one inventory item already exists.
		# Reuse it.
		# ---------------------------------------------------------
		if inventory_item_count == 1:
			inventory_item_id = self._extract_inventory_item_id(inventory_items[0])
			if not inventory_item_id:
				raise ValueError(
					f"Medusa variant {variant_id} has exactly one "
					"inventory item relation, but its ID could not "
					"be resolved."
				)
			return inventory_item_id

		# ---------------------------------------------------------
		# CASE 3:
		# Multiple inventory items exist.
		# Never guess.
		# ---------------------------------------------------------
		if inventory_item_count > 1:
			inventory_item_ids = [
				inventory_item_id
				for item in inventory_items
				if (inventory_item_id := self._extract_inventory_item_id(item))
			]
			raise ValueError(
				f"Medusa variant {variant_id} has "
				f"{inventory_item_count} inventory items linked "
				f"({', '.join(inventory_item_ids) or 'unknown IDs'}). "
				"Inventory sync requires exactly one inventory item."
			)

		# ---------------------------------------------------------
		# CASE 1:
		# No inventory item exists.
		#
		# Create it, then explicitly link it to the variant.
		# (Medusa v2 does not create the link as part of item
		# creation the way v1 did with a "variant_id" field.)
		# ---------------------------------------------------------
		payload = self._build_inventory_item_payload(
			variant,
			product_title=product_title,
		)
		inventory_item = self.create_inventory_item(payload)
		inventory_item_id = self._extract_inventory_item_id(inventory_item)
		if not inventory_item_id:
			raise ValueError(f"Medusa inventory item creation returned no ID for variant {variant_id}.")

		product_id = variant.get("product_id")
		if not product_id:
			raise ValueError(
				f"Inventory item {inventory_item_id} was created for "
				f"variant {variant_id}, but the variant has no "
				f"product_id, so it could not be linked."
			)

		self.link_inventory_item_to_variant(
			product_id,
			variant_id,
			inventory_item_id,
		)

		return inventory_item_id

	@staticmethod
	def _get_variant_inventory_items(variant: dict) -> list[dict]:
		"""Return all inventory item relations from a variant."""
		items = variant.get("inventory_items")
		if not items:
			return []
		if isinstance(items, dict):
			items = [items]
		return [item for item in items if isinstance(item, dict)]

	@staticmethod
	def _extract_inventory_item_id(item: dict) -> str | None:
		"""Extract inventory item ID from an inventory item relation."""
		if not isinstance(item, dict):
			return None
		if item.get("inventory_item_id"):
			return item["inventory_item_id"]
		inventory_item = item.get("inventory_item")
		if isinstance(inventory_item, dict) and inventory_item.get("id"):
			return inventory_item["id"]
		if item.get("id"):
			return item["id"]
		return None

	@staticmethod
	def _build_inventory_item_payload(
		variant: dict,
		*,
		product_title: str | None = None,
	) -> dict:
		"""Build the inventory item creation payload.
		Title priority:
		1. Variant title + Product title
		2. Variant title
		3. Product title
		4. SKU

		Example:
		    Variant title: ``Default variant``
		    Product title: ``Blue T-Shirt``
		    Result:         ``Default variant (Blue T-Shirt)``
		"""
		payload = {}
		variant_title = (variant.get("title") or "").strip()
		product_title = (product_title or "").strip()
		sku = (variant.get("sku") or "").strip()

		if sku:
			payload["sku"] = sku

		if variant_title and product_title:
			payload["title"] = f"{variant_title} ({product_title})"
		elif variant_title:
			payload["title"] = variant_title
		elif product_title:
			payload["title"] = product_title
		elif sku:
			payload["title"] = sku

		if variant.get("metadata") is not None:
			payload["metadata"] = variant["metadata"]

		return payload
