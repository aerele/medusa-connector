# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Inventory Item webhooks (Medusa Inventory module → ERPNext Item).

Medusa Admin Inventory UI (``/app/inventory``) edits the Inventory Item model
(``hs_code``, dimensions, material, origin_country, mid_code, sku, …), not the
Product catalog fields.

Medusa Inventory *module* emits namespaced events
(``inventory.inventory-item.updated``). The Medusa forwarder maps those to the
connector short names used for webhook subscriptions:

* ``inventory-item.created``
* ``inventory-item.updated``
* ``inventory-item.deleted``

Payload is typically ``{"id": "iitem_…"}``. We fetch the full Inventory Item
via Admin API, resolve linked Product Variants / ERPNext mappings, and apply
inventory fields onto the matching Item(s).
"""

from __future__ import annotations

import time

import frappe

from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.medusa.product import ProductService
from medusa_connector.product.mapper import ProductMapper
from medusa_connector.product.sync import ProductSync
from medusa_connector.utils.sync_guard import inbound_sync
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register

_RESOLVE_ATTEMPTS = 3
_RESOLVE_DELAY_SEC = 0.75


@register("inventory-item.created", "inventory-item.updated", "inventory-item.deleted")
class InventoryItemHandler(BaseHandler):
	"""Sync Medusa Inventory Item field changes to ERPNext Items."""

	def __init__(
		self,
		inventory: InventoryService | None = None,
		products: ProductService | None = None,
		sync: ProductSync | None = None,
	) -> None:
		self.inventory = inventory or InventoryService()
		self.products = products or ProductService()
		self.sync = sync or ProductSync()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		inventory_item_id = event.entity_id or (entity or {}).get("id")
		if not inventory_item_id:
			return f"{event.name}: missing inventory item id"

		if event.name == "inventory-item.deleted":
			return self._handle_delete(inventory_item_id)

		inv = self._fetch_inventory_item(inventory_item_id)
		if not inv or not inv.get("id"):
			return f"{event.name}: inventory item {inventory_item_id} not found in Medusa"

		item_codes = self._resolve_erpnext_item_codes(inv)
		if not item_codes:
			# Try full product re-sync when we can discover product_id (creates mapping).
			product_ids = self._product_ids_from_inventory(inv)
			synced = []
			for product_id in product_ids:
				synced.append(self._resync_product(product_id))
				# Re-resolve after product sync (mapping should now hold inventory id).
			item_codes = self._resolve_erpnext_item_codes(inv)
			if not item_codes and not synced:
				return (
					f"{event.name}: no ERPNext Item for inventory {inventory_item_id} "
					f"(sku={inv.get('sku') or '-'})"
				)

		updated = []
		with inbound_sync():
			for item_code in item_codes:
				if not frappe.db.exists("Item", item_code):
					continue
				item = frappe.get_doc("Item", item_code)
				changed = self.sync.apply_inventory_item_fields(item, inv)
				if changed:
					self.sync._save_item(item)
					updated.append(item_code)
				# Keep mapping inventory id warm for next events.
				self._link_mapping_inventory_id(item_code, inventory_item_id, inv)

		if not updated and not item_codes:
			return f"{event.name}: no matching Item for {inventory_item_id}"
		return (
			f"{event.name}: inventory {inventory_item_id} → "
			f"{', '.join(updated) if updated else 'no field changes'} "
			f"(items={', '.join(item_codes) or '-'})"
		)

	def _handle_delete(self, inventory_item_id: str) -> str:
		"""Clear inventory id on mappings; do not delete ERPNext Items."""
		names = frappe.get_all(
			"Medusa Item Mapping",
			filters={"medusa_inventory_item_id": inventory_item_id},
			pluck="name",
		)
		for name in names:
			frappe.db.set_value(
				"Medusa Item Mapping",
				name,
				"medusa_inventory_item_id",
				"",
				update_modified=False,
			)
		return f"inventory-item.deleted: cleared mapping inventory id ({len(names)} row(s))"

	def _fetch_inventory_item(self, inventory_item_id: str) -> dict:
		last_err = None
		for attempt in range(_RESOLVE_ATTEMPTS):
			try:
				inv = self.inventory.get_inventory_item(inventory_item_id)
				if inv and inv.get("id"):
					return inv
			except Exception as exc:
				last_err = exc
				frappe.logger("medusa_connector").warning(
					f"inventory-item fetch attempt {attempt + 1} for {inventory_item_id}: {exc}"
				)
			if attempt + 1 < _RESOLVE_ATTEMPTS:
				time.sleep(_RESOLVE_DELAY_SEC)
		if last_err:
			raise last_err
		return {}

	def _product_ids_from_inventory(self, inv: dict) -> list[str]:
		ids: list[str] = []
		for variant in inv.get("variants") or []:
			if not isinstance(variant, dict):
				continue
			pid = variant.get("product_id") or (variant.get("product") or {}).get("id")
			if pid and pid not in ids:
				ids.append(pid)
		# When Admin returns empty variants, recover product_id from existing mappings.
		if not ids:
			filters = []
			if inv.get("id"):
				filters.append({"medusa_inventory_item_id": inv["id"]})
			sku = (inv.get("sku") or "").strip()
			if sku:
				filters.append({"sku": sku})
			for filt in filters:
				for pid in frappe.get_all(
					"Medusa Item Mapping",
					filters=filt,
					pluck="medusa_product_id",
				):
					if pid and pid not in ids:
						ids.append(pid)
		return ids

	def _resync_product(self, product_id: str) -> str:
		product = self.products.get_product(product_id)
		mapped = ProductMapper().map(product)
		result = self.sync.sync(mapped, force=True)
		return result.get("item_code") or product_id

	def _resolve_erpnext_item_codes(self, inv: dict) -> list[str]:
		"""Map Inventory Item → ERPNext Item codes via mapping, variants, or SKU."""
		codes: list[str] = []
		seen: set[str] = set()

		def _add(code: str | None) -> None:
			if code and code not in seen and frappe.db.exists("Item", code):
				seen.add(code)
				codes.append(code)

		inv_id = inv.get("id")
		if inv_id:
			for row in frappe.get_all(
				"Medusa Item Mapping",
				filters={"medusa_inventory_item_id": inv_id},
				pluck="erpnext_item_code",
			):
				_add(row)

		for variant in inv.get("variants") or []:
			if not isinstance(variant, dict):
				continue
			vid = variant.get("id")
			if vid:
				code = frappe.db.get_value(
					"Medusa Item Mapping",
					{"medusa_variant_id": vid},
					"erpnext_item_code",
				)
				_add(code)
			# Fallback: variant SKU / metadata erpnext code
			sku = variant.get("sku")
			if sku:
				_add(frappe.db.get_value("Medusa Item Mapping", {"sku": sku}, "erpnext_item_code"))
				_add(sku if frappe.db.exists("Item", sku) else None)

		sku = (inv.get("sku") or "").strip()
		if sku:
			_add(frappe.db.get_value("Medusa Item Mapping", {"sku": sku}, "erpnext_item_code"))
			_add(sku if frappe.db.exists("Item", sku) else None)
			# Admin product-variants by SKU when inventory has no embedded variants.
			if not codes:
				try:
					resp = self.products.client.execute_rest(
						"GET",
						"/admin/product-variants",
						params={"sku": sku, "limit": 5},
					)
					for variant in (resp or {}).get("variants") or []:
						vid = variant.get("id")
						if vid:
							_add(
								frappe.db.get_value(
									"Medusa Item Mapping",
									{"medusa_variant_id": vid},
									"erpnext_item_code",
								)
							)
						_add(
							variant.get("sku") if frappe.db.exists("Item", variant.get("sku") or "") else None
						)
				except Exception:
					pass

		return codes

	def _link_mapping_inventory_id(self, item_code: str, inventory_item_id: str, inv: dict) -> None:
		name = frappe.db.get_value("Medusa Item Mapping", {"erpnext_item_code": item_code}, "name")
		if not name:
			return
		frappe.db.set_value(
			"Medusa Item Mapping",
			name,
			"medusa_inventory_item_id",
			inventory_item_id,
			update_modified=False,
		)
