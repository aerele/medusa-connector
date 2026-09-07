# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import secrets
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, now_datetime

from medusa_connector.constants import DEFAULT_PAGE_LIMIT
from medusa_connector.webhook.util import receiver_base_url

# Re-check connection when enable / URL / mode / API key change.
CONNECTION_VERIFY_FIELDS = (
	"enabled",
	"medusa_base_url",
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
		delivery_note_series: DF.Literal[None]
		enabled: DF.Check
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
		old_orders_from: DF.Datetime | None
		old_orders_to: DF.Datetime | None
		price_list: DF.Link | None
		sales_invoice_series: DF.Literal[None]
		sales_order_series: DF.Literal[None]
		shipping_item: DF.Link | None
		sync_delivery_note: DF.Check
		sync_new_item_as_published: DF.Check
		sync_old_orders: DF.Check
		sync_sales_invoice: DF.Check
		update_erpnext_stock_levels_to_medusa: DF.Check
		update_medusa_item_on_update: DF.Check
		upload_erpnext_items: DF.Check
		upload_variants_as_items: DF.Check
		warehouse: DF.Link | None
		warehouse_mapping: DF.Table[MedusaWarehouseMapping]
		webhook_plugin_status: DF.Literal["Unknown", "Installed", "Not Installed", "Error"]
		webhook_receiver_url: DF.SmallText | None
		webhook_secret: DF.Password | None
		webhook_subscriptions: DF.Table[MedusaWebhookRegistration]
	# end: auto-generated types

	def validate(self) -> None:
		self._normalize_configured_urls()
		self.webhook_receiver_url = receiver_base_url()

		if not self.enabled:
			self._mark_disconnected()
			return

		if self._connection_settings_changed():
			self._verify_connection()

		self._seed_warehouse_mapping_if_needed()
		self._validate_inventory_settings()
		self._validate_old_orders_settings()

	def on_update(self) -> None:
		if self.flags.get("ignore_webhook_sync") or not self.enabled:
			return

		if cint(self.sync_old_orders) and self.has_value_changed("sync_old_orders"):
			frappe.enqueue(
				"medusa_connector.order.sync.sync_old_orders",
				queue="long",
				timeout=3600,
				enqueue_after_commit=True,
				job_id="medusa-sync-old-orders",
				deduplicate=True,
			)
			frappe.msgprint(
				_("Order sync has been queued and will start shortly."),
				title=_("Sync Orders"),
				indicator="green",
				alert=True,
			)

	# ------------------------------------------------------------------
	# Warehouse ↔ Medusa location mapping
	# ------------------------------------------------------------------

	def get_erpnext_to_medusa_wh_mapping(self) -> dict[str, str]:
		"""Return ``{erpnext_warehouse: medusa_location_id}`` for enabled rows."""
		return {
			row.erpnext_warehouse: row.medusa_location_id
			for row in self.warehouse_mapping or []
			if row.enabled and row.erpnext_warehouse and row.medusa_location_id
		}

	def get_medusa_to_erpnext_wh_mapping(self) -> dict[str, str]:
		"""Return ``{medusa_location_id: erpnext_warehouse}`` for enabled rows."""
		return {
			row.medusa_location_id: row.erpnext_warehouse
			for row in self.warehouse_mapping or []
			if row.enabled and row.erpnext_warehouse and row.medusa_location_id
		}

	def get_erpnext_warehouses(self) -> list[str]:
		return list(self.get_erpnext_to_medusa_wh_mapping().keys())

	@frappe.whitelist()
	def fetch_medusa_locations(self) -> None:
		"""Load Medusa stock locations into the warehouse mapping table."""
		if not self.enabled:
			frappe.throw(_("Please enable the Medusa Connector first."), title=_("Medusa Connector"))

		try:
			from medusa_connector.medusa.client import MedusaClient

			locations = _list_all_stock_locations(MedusaClient(settings=self))
		except Exception as exc:
			_throw_medusa_api_error(exc)

		if not locations:
			frappe.msgprint(_("No stock locations were found in Medusa."), indicator="orange")
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

		frappe.msgprint(
			_(
				"Loaded {0} Medusa stock location(s). Map each location to an ERPNext warehouse and save."
			).format(len(self.warehouse_mapping)),
			indicator="green",
			alert=True,
		)

	def _seed_warehouse_mapping_if_needed(self) -> None:
		"""Seed one mapping row from defaults when inventory is enabled and the table is empty."""
		if not self.update_erpnext_stock_levels_to_medusa or self.warehouse_mapping:
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

	def _validate_old_orders_settings(self) -> None:
		if not cint(self.sync_old_orders):
			return

		title = _("Sync Orders")
		if not self.old_orders_from or not self.old_orders_to:
			frappe.throw(_("Please set both From and To dates."), title=title)
		if get_datetime(self.old_orders_from) > get_datetime(self.old_orders_to):
			frappe.throw(_("From date cannot be after To date."), title=title)
		if not self.company:
			frappe.throw(_("Please set Company before syncing orders."), title=title)
		if not self.warehouse:
			frappe.throw(_("Please set Default Warehouse before syncing orders."), title=title)

	def _validate_inventory_settings(self) -> None:
		if not self.update_erpnext_stock_levels_to_medusa:
			return

		title = _("Inventory Sync")
		enabled_rows = [row for row in self.warehouse_mapping or [] if row.enabled]

		if not enabled_rows:
			frappe.throw(
				_(
					"Add at least one enabled Warehouse Mapping row with an ERPNext Warehouse "
					"and Medusa Location ID. Use Fetch Medusa Locations, then map each location."
				),
				title=title,
			)

		warehouses = []
		locations = []

		for row in enabled_rows:
			if not row.medusa_location_id:
				frappe.throw(
					_("Medusa Location ID is required on enabled Warehouse Mapping rows."),
					title=title,
				)

			if not row.erpnext_warehouse:
				frappe.throw(
					_("ERPNext Warehouse is required for Medusa location {0}.").format(
						row.medusa_location_id
					),
					title=title,
				)

			warehouses.append(row.erpnext_warehouse)
			locations.append(row.medusa_location_id)

		if len(warehouses) != len(set(warehouses)):
			frappe.throw(
				_("Each ERPNext Warehouse can only be used once in Warehouse Mapping."),
				title=title,
			)

		if len(locations) != len(set(locations)):
			frappe.throw(
				_("Each Medusa Location can only be used once in Warehouse Mapping."),
				title=title,
			)

	def _normalize_configured_urls(self) -> None:
		"""Normalize configured Medusa URLs before validation."""
		if self.medusa_base_url:
			cleaned = self._normalize_url(self.medusa_base_url)
			if cleaned != self.medusa_base_url:
				self.medusa_base_url = cleaned

	def _connection_settings_changed(self) -> bool:
		before = self.get_doc_before_save()
		if not before:
			return True
		return any(self.has_value_changed(f) for f in CONNECTION_VERIFY_FIELDS)

	def _mark_disconnected(self) -> None:
		self.connection_status = "Disconnected"
		self.last_connection_test = now_datetime()
		self.last_connection_message = _("Connector is disabled.")
		for fieldname in (
			"medusa_store_id",
			"default_sales_channel_id",
			"default_currency",
			"default_region_id",
			"default_location_id",
			"last_store_defaults_sync",
			"webhook_secret",
			"webhook_receiver_url",
			"webhook_plugin_status",
			"last_webhook_sync",
			"last_webhook_sync_message",
			"last_product_sync",
			"last_product_sync_message",
			"last_inventory_sync",
		):
			self.set(fieldname, None)

		self.set("webhook_subscriptions", [])
		self.set("warehouse_mapping", [])

	def _normalize_url(self, url_str: str) -> str:
		if not url_str:
			return url_str

		url_str = url_str.strip()
		parse_url = url_str if "://" in url_str else f"https://{url_str}"
		parsed = urlparse(parse_url)

		if not parsed.netloc:
			frappe.throw(_("Please enter a valid Medusa Base URL."))

		scheme = parsed.scheme
		host = parsed.netloc

		if host.startswith(("localhost", "127.0.0.1")):
			scheme = "http"

		return f"{scheme}://{host}"

	def _verify_connection(self) -> None:
		"""Health-check Medusa and refresh store defaults during validate."""
		from medusa_connector.medusa.client import MedusaClient
		from medusa_connector.utils.store_defaults import refresh_store_defaults

		try:
			client = MedusaClient(settings=self)
			client.health_check()
			defaults = refresh_store_defaults(client)
		except Exception as exc:
			_throw_medusa_api_error(exc)

		self.medusa_store_id = defaults.get("medusa_store_id") or ""
		self.default_sales_channel_id = defaults.get("default_sales_channel_id") or ""
		self.default_currency = defaults.get("default_currency") or ""
		self.default_region_id = defaults.get("default_region_id") or ""
		self.default_location_id = defaults.get("default_location_id") or ""
		self.last_store_defaults_sync = now_datetime()
		self.connection_status = "Connected"
		self.last_connection_test = now_datetime()
		self.last_connection_message = _("Connected successfully.")


def _throw_medusa_api_error(exc: Exception) -> None:
	"""Raise a clear user-facing error for Medusa API failures."""
	if isinstance(exc, requests.HTTPError):
		status_code = exc.response.status_code if exc.response is not None else None

		if status_code in (401, 403):
			frappe.throw(
				_("Unable to sign in to Medusa. Please check the Admin API Key."),
				title=_("Authentication Failed"),
			)

	frappe.throw(
		_(str(exc)) if str(exc) else _("Could not connect to Medusa. Please check the Base URL."),
		title=_("Connection Failed"),
	)


def _list_all_stock_locations(client) -> list[dict]:
	"""Paginate Medusa stock locations."""
	locations: list[dict] = []
	offset, limit = 0, DEFAULT_PAGE_LIMIT
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
	"""Reconcile Medusa webhooks with the current settings."""
	from medusa_connector.medusa.webhook_sync import WebhookSyncService

	return WebhookSyncService().sync_webhooks()


@frappe.whitelist()
def regenerate_webhook_secret() -> str:
	"""Generate a new webhook secret. Re-sync webhooks after this change."""
	doc = frappe.get_single("Medusa Settings")
	secret = secrets.token_urlsafe(32)
	doc.webhook_secret = secret
	doc.webhook_plugin_status = "Unknown"
	doc.last_webhook_sync = None
	doc.last_webhook_sync_message = _("Webhook secret changed. Please sync webhooks.")
	doc.set("webhook_subscriptions", [])
	doc.save()
	return secret
