# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Guards against Medusa ↔ ERPNext product feedback loops.

Primary loop (observed in production):

1. Medusa ``product.updated`` webhook → ProductSync saves template Item
2. ERPNext ``Item.on_update`` → ``update_variants()`` re-saves every variant
   **without** ``flags.from_medusa``
3. Item hooks export each variant → ``POST /admin/products/...``
4. Medusa emits more product/variant webhooks → back to (1)

Mitigations:
- Request-local inbound flag (``frappe.flags.medusa_inbound_sync``)
- ``dont_update_variants`` on template saves from Medusa
- Short-lived cache keys to suppress export after inbound and import after outbound
- Coalesce concurrent full product resyncs for the same product id
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe

# How long (seconds) to suppress the reverse direction after a sync.
INBOUND_EXPORT_SUPPRESS_SEC = 90
OUTBOUND_IMPORT_SUPPRESS_SEC = 45
PRODUCT_SYNC_CLAIM_SEC = 30

INBOUND_FLAG = "medusa_inbound_sync"


def is_inbound_sync() -> bool:
	"""True while a Medusa → ERPNext product sync is running in this request/job."""
	return bool(frappe.flags.get(INBOUND_FLAG))


@contextmanager
def inbound_sync():
	"""Mark the current job as inbound so Item export hooks no-op."""
	previous = frappe.flags.get(INBOUND_FLAG)
	frappe.flags[INBOUND_FLAG] = True
	try:
		yield
	finally:
		if previous:
			frappe.flags[INBOUND_FLAG] = previous
		else:
			frappe.flags.pop(INBOUND_FLAG, None)


def mark_product_imported(product_id: str, item_codes: list[str] | None = None) -> None:
	"""After Medusa → ERPNext, suppress ERPNext → Medusa for related items briefly."""
	if not product_id:
		return
	cache = frappe.cache()
	cache.set_value(
		f"medusa:imported_product:{product_id}",
		1,
		expires_in_sec=INBOUND_EXPORT_SUPPRESS_SEC,
	)
	for code in item_codes or []:
		if code:
			cache.set_value(
				f"medusa:skip_export_item:{code}",
				1,
				expires_in_sec=INBOUND_EXPORT_SUPPRESS_SEC,
			)


def mark_product_exported(product_id: str) -> None:
	"""After ERPNext → Medusa, suppress inbound webhooks for this product briefly."""
	if not product_id:
		return
	frappe.cache().set_value(
		f"medusa:exported_product:{product_id}",
		1,
		expires_in_sec=OUTBOUND_IMPORT_SUPPRESS_SEC,
	)


def should_skip_export_for_item(item_code: str | None = None) -> bool:
	"""Whether Item hooks should skip Medusa upload."""
	if is_inbound_sync():
		return True
	if item_code and frappe.cache().get_value(f"medusa:skip_export_item:{item_code}"):
		return True
	return False


def should_skip_inbound_for_product(product_id: str | None) -> bool:
	"""Whether inbound webhooks for this product should be ignored (echo of our export)."""
	if not product_id:
		return False
	return bool(frappe.cache().get_value(f"medusa:exported_product:{product_id}"))


def try_claim_product_sync(product_id: str) -> bool:
	"""Return True if this worker should run a full product resync.

	Uses a short Redis lock so one product.updated event and multiple
	product-variant.updated events for the same product are processed only once.
	"""
	if not product_id:
		return True
	key = f"medusa:product_sync_claim:{product_id}"
	cache = frappe.cache()
	# Redis SET NX via frappe cache: get then set is racy but good enough with short TTL.
	if cache.get_value(key):
		return False
	cache.set_value(key, 1, expires_in_sec=PRODUCT_SYNC_CLAIM_SEC)
	return True
