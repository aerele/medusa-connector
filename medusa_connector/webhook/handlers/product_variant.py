# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Product-variant webhooks.

Medusa often emits ``product-variant.updated`` together with ``product.updated``
(and once per variant). A full product re-sync on every variant event multiplies
work and — when combined with ERPNext ``update_variants`` — caused export loops.

Policy:
- ``product-variant.created`` / ``.updated``: no-op if product is already claimed
  for sync, otherwise one coalesced parent product re-sync.
- ``product-variant.deleted``: still re-sync parent (or orphan mapping).
"""

from __future__ import annotations

import frappe

from medusa_connector.mapper.product_mapper import ProductMapper
from medusa_connector.medusa.product import ProductService
from medusa_connector.sync.product_sync import ProductSync
from medusa_connector.utils.sync_guard import (
	should_skip_inbound_for_product,
	try_claim_product_sync,
)
from medusa_connector.webhook.handlers.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("product-variant.created", "product-variant.updated", "product-variant.deleted")
class ProductVariantHandler(BaseHandler):
	"""Keep ERPNext variants aligned by re-syncing the parent Medusa product."""

	def __init__(
		self,
		service: ProductService | None = None,
		mapper: ProductMapper | None = None,
		sync: ProductSync | None = None,
	) -> None:
		self.service = service or ProductService()
		self.mapper = mapper or ProductMapper()
		self.sync_service = sync or ProductSync()

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		variant_id = event.entity_id
		product_id = self._resolve_product_id(entity, variant_id)

		if event.name == "product-variant.deleted":
			if product_id:
				return self._resync_product(product_id, event.name, force=True)
			# Best-effort: mark mapping orphaned by variant id alone.
			name = frappe.db.exists("Medusa Item Mapping", {"medusa_variant_id": variant_id})
			if name:
				frappe.db.set_value("Medusa Item Mapping", name, "status", "Orphaned")
				item_code = frappe.db.get_value("Medusa Item Mapping", name, "erpnext_item_code")
				if item_code and frappe.db.exists("Item", item_code):
					item = frappe.get_doc("Item", item_code)
					item.disabled = 1
					item.flags.from_medusa = True
					item.flags.dont_update_variants = True
					item.save(ignore_permissions=True)
				return f"{event.name}: orphaned {variant_id}"
			return f"{event.name}: no mapping for {variant_id}"

		if not product_id:
			raise ValueError(f"Cannot resolve product id for variant {variant_id}")

		# Prefer product.updated for catalog edits; coalesce variant storms.
		if should_skip_inbound_for_product(product_id):
			return f"{event.name}: skipped (outbound export echo for {product_id})"
		if not try_claim_product_sync(product_id):
			return f"{event.name}: skipped (already syncing product {product_id})"

		return self._resync_product(product_id, event.name, force=True)

	def _resync_product(self, product_id: str, event_name: str, *, force: bool = False) -> str:
		if not force and should_skip_inbound_for_product(product_id):
			return f"{event_name}: skipped (outbound export echo for {product_id})"
		product = self.service.get_product(product_id)
		mapped = self.mapper.map(product)
		result = self.sync_service.sync(mapped, force=True)
		return f"{event_name}: {result['action']} {result['item_code']} (via product {product_id})"

	@staticmethod
	def _resolve_product_id(entity: dict, variant_id: str | None) -> str | None:
		if not entity:
			entity = {}
		product_id = entity.get("product_id") or (entity.get("product") or {}).get("id")
		if product_id:
			return product_id
		if variant_id:
			return frappe.db.get_value(
				"Medusa Item Mapping",
				{"medusa_variant_id": variant_id},
				"medusa_product_id",
			)
		return None
