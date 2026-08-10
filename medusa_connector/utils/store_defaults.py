# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Fetch and persist Medusa store defaults onto Medusa Settings.

Uses Admin API:
- ``GET /admin/stores`` for store defaults.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from medusa_connector.constants import SETTING_DOCTYPE

STORE_DEFAULT_FIELDS = (
	"default_sales_channel_id",
	"default_currency",
	"default_region_id",
	"default_location_id",
	"medusa_store_id",
)


def refresh_store_defaults(client=None) -> dict:
	"""Pull store defaults from Medusa and write them to Medusa Settings."""
	from medusa_connector.medusa.client import MedusaClient

	client = client or MedusaClient()
	defaults = {
		"medusa_store_id": None,
		"default_sales_channel_id": None,
		"default_currency": None,
		"default_region_id": None,
		"default_location_id": None,
	}

	stores_resp = client.execute_rest("GET", "/admin/stores")
	stores = (stores_resp or {}).get("stores") or []
	store = stores[0] if stores else None

	if store:
		defaults["medusa_store_id"] = store.get("id")
		defaults["default_sales_channel_id"] = store.get("default_sales_channel_id")
		defaults["default_region_id"] = store.get("default_region_id")
		defaults["default_location_id"] = store.get("default_location_id")
		defaults["default_currency"] = _currency_from_store(store)

	if defaults["default_currency"]:
		defaults["default_currency"] = str(defaults["default_currency"]).lower()

	_persist_defaults(defaults)

	# Clear process-level caches used by export helpers.
	for key in (
		"medusa_default_currency_code",
		"medusa_default_sales_channel_id",
		"medusa_store_defaults",
	):
		try:
			frappe.cache().delete_value(key)
		except Exception as exc:
			# Log cache deletion failures but don't break the flow
			frappe.log_error(
				f"Failed to clear cache key '{key}': {exc}",
				"Medusa Cache Deletion Failed",
			)
			# Continue with other cache keys

	return defaults


def get_store_defaults(*, refresh_if_empty: bool = True) -> dict:
	"""Return cached/settings store defaults; optionally refresh once if empty."""
	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	defaults = {
		"medusa_store_id": settings.get("medusa_store_id"),
		"default_sales_channel_id": settings.get("default_sales_channel_id"),
		"default_currency": (settings.get("default_currency") or "").lower() or None,
		"default_region_id": settings.get("default_region_id"),
		"default_location_id": settings.get("default_location_id"),
	}

	if refresh_if_empty and settings.enabled and not defaults["default_currency"]:
		try:
			defaults = refresh_store_defaults()
		except Exception:
			frappe.log_error(
				title="Medusa: refresh store defaults failed",
				message=frappe.get_traceback(with_context=True),
			)

	return defaults


def _persist_defaults(defaults: dict) -> None:
	settings = frappe.get_single(SETTING_DOCTYPE)

	# Avoid recursive webhook sync / validate side-effects.
	settings.flags.ignore_webhook_sync = True

	values = {
		"medusa_store_id": defaults.get("medusa_store_id") or "",
		"default_sales_channel_id": defaults.get("default_sales_channel_id") or "",
		"default_currency": defaults.get("default_currency") or "",
		"default_region_id": defaults.get("default_region_id") or "",
		"default_location_id": defaults.get("default_location_id") or "",
		"last_store_defaults_sync": now_datetime(),
	}

	# Use db_set to avoid full validate when only refreshing defaults.
	for field, value in values.items():
		if settings.meta.has_field(field):
			settings.db_set(field, value, update_modified=False, commit=False)


def _currency_from_store(store: dict) -> str | None:
	for row in store.get("supported_currencies") or []:
		if row.get("is_default") and row.get("currency_code"):
			return str(row["currency_code"]).lower()

	return None
