# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from medusa_connector.webhook.util import receiver_base_url

# Webhook re-sync after save when these change. Status/result fields are excluded
# so the sync service write-back does not re-enqueue itself.
SYNC_TRIGGER_FIELDS = (
	"enabled",
	"connection_mode",
	"medusa_base_url",
	"admin_api_key",
	"webhook_secret",
)

# Connection probe + store-defaults refresh on enable and when reachability
# settings change while the connector stays enabled.
CONNECTION_VERIFY_FIELDS = (
	"enabled",
	"connection_mode",
	"medusa_base_url",
	"graphql_url",
	"admin_api_key",
)


class MedusaSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from medusa_connector.medusa_connector.doctype.medusa_warehouse_mapping.medusa_warehouse_mapping import (
			MedusaWarehouseMapping,
		)
		from medusa_connector.medusa_connector.doctype.medusa_webhook_registration.medusa_webhook_registration import (
			MedusaWebhookRegistration,
		)

		add_shipping_as_item: DF.Check
		admin_api_key: DF.Password | None
		cash_bank_account: DF.Link | None
		company: DF.Link | None
		connection_mode: DF.Literal["REST", "GraphQL"]
		connection_status: DF.Literal["Unknown", "Disconnected", "Connected", "Auth Failed", "Error"]
		consolidate_taxes: DF.Check
		cost_center: DF.Link | None
		customer_group: DF.Link | None
		default_currency: DF.Data | None
		default_customer: DF.Link | None
		default_location_id: DF.Data | None
		default_region_id: DF.Data | None
		default_sales_channel_id: DF.Data | None
		default_sales_tax_account: DF.Link | None
		default_shipping_charges_account: DF.Link | None
		default_stock_uom: DF.Link | None
		enabled: DF.Check
		graphql_url: DF.Data | None
		inventory_sync_frequency: DF.Literal["5", "10", "15", "30", "60"]
		item_group: DF.Link | None
		last_connection_message: DF.SmallText | None
		last_connection_test: DF.Datetime | None
		last_inventory_sync: DF.Datetime | None
		last_product_sync: DF.Datetime | None
		last_product_sync_message: DF.SmallText | None
		last_store_defaults_sync: DF.Datetime | None
		last_webhook_sync: DF.Datetime | None
		last_webhook_sync_message: DF.SmallText | None
		medusa_base_url: DF.Data | None
		medusa_store_id: DF.Data | None
		on_item_delete: DF.Literal["Draft", "Delete"]
		price_list: DF.Link | None
		sales_invoice_series: DF.Literal[None]
		sales_order_series: DF.Literal[None]
		shipping_item: DF.Link | None
		sync_new_item_as_published: DF.Check
		sync_sales_invoice: DF.Check
		update_erpnext_stock_levels_to_medusa: DF.Check
		update_medusa_item_on_update: DF.Check
		upload_erpnext_items: DF.Check
		upload_variants_as_items: DF.Check
		verify_signatures: DF.Check
		warehouse: DF.Link | None
		warehouse_mapping: DF.Table[MedusaWarehouseMapping]
		webhook_plugin_status: DF.Literal["Unknown", "Installed", "Not Installed", "Error"]
		webhook_receiver_url: DF.SmallText | None
		webhook_secret: DF.Password | None
		webhook_signature_header: DF.Data | None
		webhook_subscriptions: DF.Table[MedusaWebhookRegistration]
	# end: auto-generated types

	def validate(self) -> None:
		self._normalize_configured_urls()
		self.webhook_receiver_url = receiver_base_url()

		if not self.enabled:
			self._mark_disconnected()
			return

		# Shopify Setting.validate does the same when the integration is enabled:
		# ensure Customer/Address identity custom fields exist (install/migrate also
		# create them; this keeps saves self-healing if fields were removed).
		from medusa_connector.setup import setup_custom_fields

		setup_custom_fields(update=True)

		# Connection first so store defaults can populate Default Location ID
		# before inventory settings are validated.
		if self._connection_settings_changed():
			self._verify_connection()

		self._seed_warehouse_mapping_if_needed()
		self._validate_inventory_settings()

	def on_update(self) -> None:
		"""Enqueue webhook reconciliation after connection-relevant saves."""
		if self.flags.get("ignore_webhook_sync") or not self.enabled:
			return

		if any(self.has_value_changed(f) for f in SYNC_TRIGGER_FIELDS):
			frappe.enqueue(
				"medusa_connector.medusa.webhook_sync.sync_webhooks",
				queue="short",
				timeout=600,
				enqueue_after_commit=True,
				job_id="medusa-webhook-sync",
				deduplicate=True,
			)

	# ------------------------------------------------------------------
	# Warehouse ↔ Medusa location mapping (Shopify-style)
	# ------------------------------------------------------------------

	def get_erpnext_to_integration_wh_mapping(self) -> dict[str, str]:
		"""Return ``{erpnext_warehouse: medusa_location_id}`` for enabled rows."""
		return {
			row.erpnext_warehouse: row.medusa_location_id
			for row in self.warehouse_mapping or []
			if row.enabled and row.erpnext_warehouse and row.medusa_location_id
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[str, str]:
		"""Return ``{medusa_location_id: erpnext_warehouse}`` for enabled rows."""
		return {
			row.medusa_location_id: row.erpnext_warehouse
			for row in self.warehouse_mapping or []
			if row.enabled and row.erpnext_warehouse and row.medusa_location_id
		}

	def get_erpnext_warehouses(self) -> list[str]:
		return list(self.get_erpnext_to_integration_wh_mapping().keys())

	@frappe.whitelist()
	def fetch_medusa_locations(self) -> None:
		"""Populate warehouse mapping from Medusa stock locations (Shopify pattern).

		Preserves existing ERPNext warehouse links keyed by location id.
		"""
		frappe.only_for("System Manager")
		if not self.enabled:
			frappe.throw(frappe._("Enable the Medusa Connector first."))

		from medusa_connector.medusa.client import MedusaClient
		from medusa_connector.medusa.exceptions import MedusaAuthError, MedusaConnectionError

		try:
			client = MedusaClient(settings=self)
			locations = _list_all_stock_locations(client)
		except MedusaAuthError:
			frappe.throw(
				frappe._("Unable to authenticate with Medusa. Please verify the Admin API Key."),
				title=frappe._("Authentication Failed"),
			)
		except MedusaConnectionError as exc:
			frappe.throw(frappe._(str(exc)), title=frappe._("Connection Failed"))

		if not locations:
			frappe.msgprint(frappe._("No stock locations found on Medusa."), indicator="orange")
			return

		existing_wh = {
			row.medusa_location_id: row.erpnext_warehouse
			for row in self.warehouse_mapping or []
			if row.medusa_location_id
		}
		existing_enabled = {
			row.medusa_location_id: row.enabled
			for row in self.warehouse_mapping or []
			if row.medusa_location_id
		}

		self.set("warehouse_mapping", [])
		for loc in locations:
			loc_id = loc.get("id") or ""
			if not loc_id:
				continue
			self.append(
				"warehouse_mapping",
				{
					"medusa_location_id": loc_id,
					"medusa_location_name": loc.get("name") or loc_id,
					"erpnext_warehouse": existing_wh.get(loc_id) or "",
					"enabled": existing_enabled.get(loc_id, 1 if existing_wh.get(loc_id) else 0),
				},
			)

		# Do not save — leave the form dirty so the operator maps warehouses then saves
		# (same pattern as Shopify Settings.update_location_table).
		frappe.msgprint(
			frappe._("Loaded {0} Medusa stock location(s). Map ERPNext warehouses and save.").format(
				len(self.warehouse_mapping)
			),
			indicator="green",
			alert=True,
		)

	def _seed_warehouse_mapping_if_needed(self) -> None:
		"""If inventory is on and mapping is empty, seed from defaults (migration path)."""
		if not self.update_erpnext_stock_levels_to_medusa:
			return
		if self.warehouse_mapping:
			return
		if self.warehouse and self.default_location_id:
			self.append(
				"warehouse_mapping",
				{
					"medusa_location_id": self.default_location_id,
					"medusa_location_name": self.default_location_id,
					"erpnext_warehouse": self.warehouse,
					"enabled": 1,
				},
			)

	def _validate_inventory_settings(self) -> None:
		"""Require at least one valid warehouse↔location map when inventory push is on."""
		if not self.update_erpnext_stock_levels_to_medusa:
			return

		mapping = self.get_erpnext_to_integration_wh_mapping()
		if not mapping:
			frappe.throw(
				frappe._(
					"Add at least one enabled Warehouse Mapping row (ERPNext Warehouse + Medusa Location ID). "
					"Use Fetch Medusa Locations, then link each location to a warehouse."
				),
				title=frappe._("Inventory Sync"),
			)

		warehouses = list(mapping.keys())
		locations = list(mapping.values())
		if len(warehouses) != len(set(warehouses)):
			frappe.throw(
				frappe._("Each ERPNext Warehouse may appear only once in Warehouse Mapping."),
				title=frappe._("Inventory Sync"),
			)
		if len(locations) != len(set(locations)):
			frappe.throw(
				frappe._("Each Medusa Location may appear only once in Warehouse Mapping."),
				title=frappe._("Inventory Sync"),
			)

		for row in self.warehouse_mapping or []:
			if not row.enabled:
				continue
			if not row.medusa_location_id:
				frappe.throw(
					frappe._("Medusa Location ID is required on enabled Warehouse Mapping rows."),
					title=frappe._("Inventory Sync"),
				)
			if not row.erpnext_warehouse:
				frappe.throw(
					frappe._(
						"ERPNext Warehouse is required on enabled Warehouse Mapping rows (location {0})."
					).format(row.medusa_location_id),
					title=frappe._("Inventory Sync"),
				)

	def _normalize_configured_urls(self) -> None:
		if self.medusa_base_url:
			cleaned = self._normalize_url(self.medusa_base_url)
			if cleaned != self.medusa_base_url:
				self.medusa_base_url = cleaned
				frappe.msgprint(
					frappe._("Normalized Medusa Base URL to '{0}'.").format(cleaned),
					alert=True,
				)

		if self.graphql_url:
			cleaned = self._normalize_url(self.graphql_url, is_graphql=True)
			if cleaned != self.graphql_url:
				self.graphql_url = cleaned
				frappe.msgprint(
					frappe._("Normalized GraphQL URL to '{0}'.").format(cleaned),
					alert=True,
				)

	def _connection_settings_changed(self) -> bool:
		"""True on first save or when enable / URL / mode / API key change."""
		before = self.get_doc_before_save()
		if not before:
			return True
		return any(self.has_value_changed(f) for f in CONNECTION_VERIFY_FIELDS)

	def _mark_disconnected(self) -> None:
		self.connection_status = "Disconnected"
		self.last_connection_test = now_datetime()
		self.last_connection_message = frappe._("Connector disabled.")

	def _normalize_url(self, url_str: str, is_graphql: bool = False) -> str:
		if not url_str:
			return url_str

		url_str = url_str.strip()
		has_scheme = url_str.startswith(("http://", "https://"))
		scheme = "https"
		if url_str.startswith("http://"):
			scheme = "http"

		temp = url_str.split("://", 1)[1] if has_scheme else url_str
		host, _, _ = temp.partition("/")

		if not has_scheme and host.startswith(("localhost", "127.0.0.1")):
			scheme = "http"

		if is_graphql:
			return f"{scheme}://{host}/graphql"

		return f"{scheme}://{host}"

	def _verify_connection(self) -> None:
		"""Health-check Medusa and refresh store defaults onto this doc.

		Runs inside ``validate`` so a bad URL/key aborts the save instead of
		leaving the connector marked Connected against an unreachable host.
		"""
		from medusa_connector.medusa.client import MedusaClient
		from medusa_connector.medusa.exceptions import MedusaAuthError, MedusaConnectionError
		from medusa_connector.utils.store_defaults import refresh_store_defaults

		try:
			client = MedusaClient(settings=self)
			client.health_check()
			defaults = refresh_store_defaults(client, commit=False)
		except MedusaAuthError:
			frappe.throw(
				frappe._("Unable to authenticate with Medusa. Please verify the Admin API Key."),
				title=frappe._("Authentication Failed"),
			)
		except MedusaConnectionError as exc:
			frappe.throw(
				frappe._(str(exc)),
				title=frappe._("Connection Failed"),
			)

		self.medusa_store_id = defaults.get("medusa_store_id") or ""
		self.default_sales_channel_id = defaults.get("default_sales_channel_id") or ""
		self.default_currency = defaults.get("default_currency") or ""
		self.default_region_id = defaults.get("default_region_id") or ""
		self.default_location_id = defaults.get("default_location_id") or ""
		self.last_store_defaults_sync = now_datetime()
		self.connection_status = "Connected"
		self.last_connection_test = now_datetime()
		self.last_connection_message = frappe._("Connection successful ({0}).").format(client.mode)


def _list_all_stock_locations(client) -> list[dict]:
	"""Paginate ``GET /admin/stock-locations``."""
	locations: list[dict] = []
	offset, limit = 0, 100
	while True:
		resp = client.execute_rest("GET", "/admin/stock-locations", params={"limit": limit, "offset": offset})
		page = (resp or {}).get("stock_locations") or []
		locations.extend(page)
		count = (resp or {}).get("count")
		offset += limit
		if not page or count is None or offset >= count:
			break
	return locations


@frappe.whitelist()
def sync_webhooks() -> dict:
	"""Manually reconcile webhooks now (button on the settings form)."""
	from medusa_connector.medusa.webhook_sync import WebhookSyncService

	return WebhookSyncService().sync()


@frappe.whitelist()
def regenerate_webhook_secret() -> str:
	"""Generate a fresh HMAC/token secret. Invalidates the current registration."""
	doc = frappe.get_single("Medusa Settings")
	doc.webhook_secret = secrets.token_urlsafe(32)
	doc.save()
	frappe.db.commit()
	return frappe._("Webhook secret regenerated. Re-sync webhooks to push it to Medusa.")


@frappe.whitelist()
def test_connection() -> dict:
	"""Desk button: probe Medusa, refresh store defaults, return status."""
	from medusa_connector.medusa.client import test_connection as _test

	return _test()


@frappe.whitelist()
def refresh_store_defaults() -> dict:
	"""Desk button: re-fetch store defaults without a full connection lifecycle."""
	frappe.only_for("System Manager")
	from medusa_connector.utils.store_defaults import refresh_store_defaults as _refresh

	if not frappe.db.get_single_value("Medusa Settings", "enabled"):
		frappe.throw(frappe._("Enable the Medusa Connector first."))
	defaults = _refresh(commit=True)
	return {
		"ok": True,
		"message": frappe._("Store defaults refreshed."),
		"defaults": defaults,
	}


@frappe.whitelist()
def sync_inventory_now() -> dict:
	"""Desk button: force ERPNext → Medusa inventory push once."""
	from medusa_connector.product.inventory_export import sync_inventory_now as _sync

	return _sync()
