# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Inventory Item webhooks (Medusa Inventory module → ERPNext Item fields).

Qty is **not** written here — ERPNext is stock master. This only maps
inventory item attributes onto ERPNext Items via Ecommerce Item (variant/SKU).

Resolution is a single lookup against the existing Ecommerce Item mapping.
If no mapping exists yet (e.g. this event arrived before the product was
synced), the event is logged and skipped — it is not retried, resynced, or
guessed at here. The next product sync or a redelivered webhook resolves it.
"""

from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import get_erpnext_item

from medusa_connector.constants import MODULE_NAME
from medusa_connector.medusa.inventory import InventoryService
from medusa_connector.product.services.persistence import PersistenceService
from medusa_connector.product.sync import ProductSync
from medusa_connector.utils.sync_guard import inbound_sync
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("inventory-item.updated")
class InventoryItemHandler(BaseHandler):
	"""Sync Medusa Inventory Item field changes to ERPNext Items."""

	def __init__(
		self,
		inventory: InventoryService | None = None,
		sync: ProductSync | None = None,
		persistence: PersistenceService | None = None,
	) -> None:
		self.inventory = inventory or InventoryService()
		self.sync = sync or ProductSync()
		self.persistence = persistence or PersistenceService()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		inventory_item_id = event.entity_id or (entity or {}).get("id")
		if not inventory_item_id:
			return f"{event.name}: missing inventory item id"

		inv = self.inventory.get_inventory_item(inventory_item_id)
		if not inv or not inv.get("id"):
			return f"{event.name}: inventory item {inventory_item_id} not found in Medusa"

		items = self._resolve_erpnext_items(inv)
		if not items:
			return (
				f"{event.name}: no ERPNext Item mapping for inventory {inventory_item_id} "
				f"(sku={inv.get('sku') or '-'})"
			)

		updated = []
		with inbound_sync():
			for item in items:
				changed = self.sync.apply_inventory_item_fields(item, inv)
				if changed:
					self.persistence.save_item(item)
					updated.append(item.name)

		item_names = ", ".join(item.name for item in items)
		return (
			f"{event.name}: inventory {inventory_item_id} → "
			f"{', '.join(updated) if updated else 'no field changes'} "
			f"(items={item_names or '-'})"
		)

	def _resolve_erpnext_items(self, inv: dict) -> list:
		"""Resolve ERPNext Item documents linked to this Medusa inventory item.

		Uses ``get_erpnext_item`` — the same Ecommerce Item lookup every other
		connector on this site uses — instead of a connector-local re-query.
		A stale Ecommerce Item mapping (pointing at a deleted Item) is skipped
		rather than raised, since a dangling mapping is a data-state issue,
		not something this webhook should fail on.
		"""
		items = []
		seen: set[str] = set()

		def _add(item_doc) -> None:
			if item_doc and item_doc.name not in seen:
				seen.add(item_doc.name)
				items.append(item_doc)

		for variant in inv.get("variants") or []:
			if not isinstance(variant, dict):
				continue
			variant_id = variant.get("id")
			sku = variant.get("sku")
			if not (variant_id or sku):
				continue
			product_id = variant.get("product_id") or (variant.get("product") or {}).get("id") or ""
			try:
				_add(get_erpnext_item(MODULE_NAME, product_id, variant_id=variant_id, sku=sku))
			except frappe.DoesNotExistError:
				continue

		if not items:
			top_sku = (inv.get("sku") or "").strip()
			if top_sku:
				try:
					_add(get_erpnext_item(MODULE_NAME, "", sku=top_sku))
				except frappe.DoesNotExistError:
					pass

		return items
