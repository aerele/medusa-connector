# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Product-domain webhook handlers (Medusa → ERPNext).

Flow: receive → dispatch → ProductHandler / ProductVariantHandler →
ProductService → ProductMapper → ProductSync.
"""

from __future__ import annotations

import time

import frappe

from medusa_connector.medusa.product import ProductService
from medusa_connector.product.mapper import ProductMapper
from medusa_connector.product.sync import ProductSync
from medusa_connector.utils.sync_guard import (
	enqueue_deferred_product_resync,
	release_product_sync_claim,
	should_skip_inbound_for_product,
	try_claim_product_sync,
)
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register

# Short retries when Medusa has not yet propagated a brand-new variant.
_VARIANT_RESOLVE_ATTEMPTS = 3
_VARIANT_RESOLVE_DELAY_SEC = 0.75


def resync_product_by_id(product_id: str, reason: str | None = None) -> str | None:
	"""Background full product re-import (used after a skipped claim / race)."""
	if not product_id:
		return None
	if should_skip_inbound_for_product(product_id):
		return f"deferred resync skipped (outbound echo) {product_id}"
	if not try_claim_product_sync(product_id):
		# Still busy — try once more later without stacking many jobs.
		enqueue_deferred_product_resync(product_id, reason="claim still busy")
		return f"deferred resync re-queued {product_id}"
	try:
		service = ProductService()
		mapper = ProductMapper()
		sync = ProductSync()
		product = service.get_product(product_id)
		mapped = mapper.map(product)
		result = sync.sync(mapped, force=True)
		return (
			f"deferred resync ({reason or 'follow-up'}): "
			f"{result.get('action')} {result.get('item_code')} "
			f"status={mapped.get('raw_status')} disabled={mapped.get('disabled')}"
		)
	finally:
		release_product_sync_claim(product_id)


@register("product.created", "product.updated", "product.deleted")
class ProductHandler(BaseHandler):
	"""Hydrate, map, and synchronise Medusa products to ERPNext Items."""

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
		product_id = event.entity_id

		if event.name == "product.deleted":
			if not product_id:
				return "Deleted product without id"
			item_code = self.sync_service.handle_product_delete(product_id)
			return f"Deleted {product_id} → {item_code or 'no mapping'}"

		if not product_id:
			raise ValueError("Product webhook does not contain a product id")

		if should_skip_inbound_for_product(product_id):
			return f"{event.name}: skipped (outbound export echo for {product_id})"

		claimed = try_claim_product_sync(product_id)
		if event.name == "product.updated" and not claimed:
			# Publish often races with an earlier draft product.updated. Queue a
			# follow-up so status/disabled is not lost for 30s.
			enqueue_deferred_product_resync(product_id, reason=f"{event.name} claim busy")
			return f"{event.name}: skipped (already syncing {product_id}; deferred resync queued)"

		try:
			product = self.service.get_product(product_id)
			mapped_product = self.mapper.map(product)
			result = self.sync_service.sync(mapped_product, force=True)
			return (
				f"{event.name}: {result['action']} {result['item_code']} "
				f"(status={mapped_product.get('raw_status')} disabled={mapped_product.get('disabled')})"
			)
		finally:
			if claimed or event.name == "product.created":
				release_product_sync_claim(product_id)


@register("product-variant.created", "product-variant.updated", "product-variant.deleted")
class ProductVariantHandler(BaseHandler):
	"""Keep ERPNext variants aligned by re-syncing the parent Medusa product.

	Webhook payloads from the plugin typically only include ``{"id": "variant_…"}``.
	Parent product is resolved via Admin ``GET /admin/product-variants?id=…``
	(``product_id`` on the variant), then the full product is re-synced.
	"""

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
			name = frappe.db.exists("Medusa Item Mapping", {"medusa_variant_id": variant_id})
			if name:
				frappe.db.set_value("Medusa Item Mapping", name, "status", "Orphaned")
				item_code = frappe.db.get_value("Medusa Item Mapping", name, "erpnext_item_code")
				if item_code and frappe.db.exists("Item", item_code):
					item = frappe.get_doc("Item", item_code)
					item.disabled = 1
					item.flags.from_medusa = True
					item.flags.from_integration = True
					item.flags.dont_update_variants = True
					item.flags.ignore_mandatory = True
					item.save(ignore_permissions=True)
				return f"{event.name}: orphaned {variant_id}"
			return f"{event.name}: no mapping for {variant_id}"

		if not product_id:
			raise ValueError(
				f"Cannot resolve product id for variant {variant_id}. "
				"Fetch via Admin /admin/product-variants failed and no mapping exists yet."
			)

		if should_skip_inbound_for_product(product_id):
			return f"{event.name}: skipped (outbound export echo for {product_id})"
		if not try_claim_product_sync(product_id):
			enqueue_deferred_product_resync(product_id, reason=f"{event.name} claim busy")
			return f"{event.name}: skipped (already syncing product {product_id}; deferred resync queued)"

		try:
			return self._resync_product(product_id, event.name, force=True)
		finally:
			release_product_sync_claim(product_id)

	def _resync_product(self, product_id: str, event_name: str, *, force: bool = False) -> str:
		if not force and should_skip_inbound_for_product(product_id):
			return f"{event_name}: skipped (outbound export echo for {product_id})"
		product = self.service.get_product(product_id)
		mapped = self.mapper.map(product)
		result = self.sync_service.sync(mapped, force=True)
		return (
			f"{event_name}: {result['action']} {result['item_code']} "
			f"(via product {product_id}; status={mapped.get('raw_status')} "
			f"disabled={mapped.get('disabled')})"
		)

	def _resolve_product_id(self, entity: dict, variant_id: str | None) -> str | None:
		"""Resolve parent product id for a variant webhook.

		Order:
		1. Payload fields (``product_id`` / nested ``product.id``)
		2. Existing Medusa Item Mapping
		3. Admin API ``GET /admin/product-variants`` (with short retries for create race)
		"""
		entity = entity or {}
		product_id = entity.get("product_id") or (entity.get("product") or {}).get("id")
		if product_id:
			return product_id

		if variant_id:
			mapped = frappe.db.get_value(
				"Medusa Item Mapping",
				{"medusa_variant_id": variant_id},
				"medusa_product_id",
			)
			if mapped:
				return mapped

			# New variants: payload is often only {"id": "variant_…"}.
			return self._fetch_product_id_from_medusa(variant_id)
		return None

	def _fetch_product_id_from_medusa(self, variant_id: str) -> str | None:
		"""Look up variant via Admin API; retry briefly if not yet visible."""
		last_err = None
		for attempt in range(_VARIANT_RESOLVE_ATTEMPTS):
			try:
				variant = self.service.get_variant(variant_id)
				product_id = (variant or {}).get("product_id") or (
					(variant.get("product") or {}).get("id") if isinstance(variant, dict) else None
				)
				if product_id:
					return product_id
			except Exception as exc:
				last_err = exc
				frappe.logger("medusa_connector").warning(
					f"product-variant resolve attempt {attempt + 1} failed for {variant_id}: {exc}"
				)
			if attempt + 1 < _VARIANT_RESOLVE_ATTEMPTS:
				time.sleep(_VARIANT_RESOLVE_DELAY_SEC)

		if last_err:
			frappe.logger("medusa_connector").error(
				f"Could not resolve product for variant {variant_id}: {last_err}"
			)
		return None
