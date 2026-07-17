# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Inventory Item webhooks (Medusa Inventory module → ERPNext Item fields).

Qty is **not** written here — ERPNext is stock master. This only maps inventory
item attributes onto ERPNext Items via Ecommerce Item (variant/SKU).
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import cstr

from medusa_connector.constants import MODULE_NAME
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
			return f"inventory-item.deleted: {inventory_item_id} (no ERP qty change)"

		inv = self._fetch_inventory_item(inventory_item_id)
		if not inv or not inv.get("id"):
			return f"{event.name}: inventory item {inventory_item_id} not found in Medusa"

		item_codes = self._resolve_erpnext_item_codes(inv)
		if not item_codes:
			product_ids = self._product_ids_from_inventory(inv)
			for product_id in product_ids:
				self._resync_product(product_id)
			item_codes = self._resolve_erpnext_item_codes(inv)
			if not item_codes:
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

		return (
			f"{event.name}: inventory {inventory_item_id} → "
			f"{', '.join(updated) if updated else 'no field changes'} "
			f"(items={', '.join(item_codes) or '-'})"
		)

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
		sku = (inv.get("sku") or "").strip()
		if not ids and sku:
			from medusa_connector.product.item_mapping import (
				fetch_product_id_for_variant,
				get_medusa_product_id_from_row,
			)

			for row in frappe.get_all(
				"Ecommerce Item",
				filters={"integration": MODULE_NAME, "sku": sku},
				fields=["integration_item_code", "variant_id", "variant_of", "has_variants"],
			):
				pid = get_medusa_product_id_from_row(row, fetch_if_missing=False)
				if not pid:
					vid = row.variant_id or (
						row.integration_item_code
						if cstr(row.integration_item_code).startswith("variant_")
						else None
					)
					pid = fetch_product_id_for_variant(vid) if vid else None
				if pid and pid not in ids:
					ids.append(pid)
		return ids

	def _resync_product(self, product_id: str) -> str:
		product = self.products.get_product(product_id)
		mapped = ProductMapper().map(product)
		result = self.sync.sync(mapped, force=True)
		return result.get("item_code") or product_id

	def _resolve_erpnext_item_codes(self, inv: dict) -> list[str]:
		codes: list[str] = []
		seen: set[str] = set()

		def _add(code: str | None) -> None:
			if code and code not in seen and frappe.db.exists("Item", code):
				seen.add(code)
				codes.append(code)

		for variant in inv.get("variants") or []:
			if not isinstance(variant, dict):
				continue
			vid = variant.get("id")
			if vid:
				_add(
					frappe.db.get_value(
						"Ecommerce Item",
						{"integration": MODULE_NAME, "variant_id": vid},
						"erpnext_item_code",
					)
				)
			sku = variant.get("sku")
			if sku:
				_add(
					frappe.db.get_value(
						"Ecommerce Item",
						{"integration": MODULE_NAME, "sku": sku},
						"erpnext_item_code",
					)
				)
				_add(sku if frappe.db.exists("Item", sku) else None)

		sku = (inv.get("sku") or "").strip()
		if sku:
			_add(
				frappe.db.get_value(
					"Ecommerce Item",
					{"integration": MODULE_NAME, "sku": sku},
					"erpnext_item_code",
				)
			)
			_add(sku if frappe.db.exists("Item", sku) else None)

		return codes
