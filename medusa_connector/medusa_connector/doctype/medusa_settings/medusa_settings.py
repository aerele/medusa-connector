# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from medusa_connector.webhook.util import receiver_base_url

# Changing any of these fields invalidates the current webhook registration and
# triggers a re-sync on save. Result/status fields are deliberately excluded so
# the sync's own write-back does not re-trigger a sync (no reconciliation loop).
SYNC_TRIGGER_FIELDS = (
	"enabled",
	"connection_mode",
	"medusa_base_url",
	"admin_api_key",
	"webhook_secret",
)


class MedusaSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from medusa_connector.medusa_connector.doctype.medusa_webhook_registration.medusa_webhook_registration import (
			MedusaWebhookRegistration,
		)

		admin_api_key: DF.Password | None
		connection_mode: DF.Literal["REST", "GraphQL"]
		connection_status: DF.Literal["Unknown", "Disconnected", "Connected", "Auth Failed", "Error"]
		default_currency: DF.Data | None
		default_location_id: DF.Data | None
		default_region_id: DF.Data | None
		default_sales_channel_id: DF.Data | None
		default_stock_uom: DF.Link | None
		enabled: DF.Check
		graphql_url: DF.Data | None
		inventory_sync_frequency: DF.Literal["5", "10", "15", "30", "60"] | None
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
		on_item_delete: DF.Literal["Draft", "Delete"] | None
		price_list: DF.Link | None
		sync_new_item_as_published: DF.Check
		update_erpnext_stock_levels_to_medusa: DF.Check
		update_medusa_item_on_update: DF.Check
		upload_erpnext_items: DF.Check
		upload_variants_as_items: DF.Check
		verify_signatures: DF.Check
		warehouse: DF.Link | None
		webhook_plugin_status: DF.Literal["Unknown", "Installed", "Not Installed", "Error"]
		webhook_receiver_url: DF.SmallText | None
		webhook_secret: DF.Password | None
		webhook_subscriptions: DF.Table[MedusaWebhookRegistration]
	# end: auto-generated types

	def validate(self) -> None:
		# Clean and normalize URLs
		if self.medusa_base_url:
			original = self.medusa_base_url
			cleaned = self._normalize_url(original)
			if cleaned != original:
				self.medusa_base_url = cleaned
				frappe.msgprint(
					frappe._("Normalized Medusa Base URL to '{0}'.").format(cleaned),
					alert=True,
				)

		if self.graphql_url:
			original = self.graphql_url
			cleaned = self._normalize_url(original, is_graphql=True)
			if cleaned != original:
				self.graphql_url = cleaned
				frappe.msgprint(
					frappe._("Normalized GraphQL URL to '{0}'.").format(cleaned),
					alert=True,
				)

		# Surface the guest receiver URL so the admin can see/copy the ERP endpoint.
		self.webhook_receiver_url = receiver_base_url()

		# Auto-test only on the transition into "Enabled". A failing probe raises,
		# which aborts the save so the checkbox never persists as enabled.
		if self.enabled:
			before = self.get_doc_before_save()
			if not before or not before.enabled:
				self._verify_connection()
		else:
			# Disabling the connector reflects as a clean "Disconnected" state.
			self.connection_status = "Disconnected"
			self.last_connection_test = now_datetime()
			self.last_connection_message = frappe._("Connector disabled.")

	def _normalize_url(self, url_str: str, is_graphql: bool = False) -> str:
		if not url_str:
			return url_str

		url_str = url_str.strip()

		# Determine scheme
		has_scheme = url_str.startswith(("http://", "https://"))
		scheme = "https"
		if url_str.startswith("http://"):
			scheme = "http"

		# Remove scheme for processing
		temp = url_str.split("://", 1)[1] if has_scheme else url_str

		# Split host and path
		host, _, _ = temp.partition("/")

		# Default localhost to http
		if not has_scheme and host.startswith(("localhost", "127.0.0.1")):
			scheme = "http"

		if is_graphql:
			return f"{scheme}://{host}/graphql"

		return f"{scheme}://{host}"

	def on_update(self) -> None:
		"""After a connection-relevant change, reconcile the Medusa webhooks."""
		if self.flags.get("ignore_webhook_sync"):
			# This save came from the sync service itself — do not recurse.
			return
		if not self.enabled:
			return
		if not any(self.has_value_changed(f) for f in SYNC_TRIGGER_FIELDS):
			return
		# Run in the background so the save returns fast; enqueue after commit so the
		# job reads the persisted settings (fresh key/URL/secret).
		frappe.enqueue(
			"medusa_connector.medusa.services.webhook_sync.sync_webhooks",
			queue="short",
			enqueue_after_commit=True,
			job_id="medusa-webhook-sync",
			deduplicate=True,
		)

	def _verify_connection(self) -> None:
		"""Probe Medusa with the in-flight settings; block the save if it fails."""
		from medusa_connector.medusa.client import MedusaClient
		from medusa_connector.medusa.exceptions import MedusaAuthError, MedusaConnectionError
		from medusa_connector.utils.store_defaults import refresh_store_defaults

		try:
			client = MedusaClient(settings=self)
			# Fail fast: this probe runs synchronously inside the save request, so a
			# slow/unreachable Medusa must not stall the form for the full 30s.
			client.timeout = 10
			client.health_check()
			# Populate store defaults on the in-memory doc so they save with enable.
			defaults = refresh_store_defaults(client, commit=False)
			self.medusa_store_id = defaults.get("medusa_store_id") or ""
			self.default_sales_channel_id = defaults.get("default_sales_channel_id") or ""
			self.default_currency = defaults.get("default_currency") or ""
			self.default_region_id = defaults.get("default_region_id") or ""
			self.default_location_id = defaults.get("default_location_id") or ""
			self.last_store_defaults_sync = now_datetime()
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
		# Record the successful probe alongside the save.
		self.connection_status = "Connected"
		self.last_connection_test = now_datetime()
		self.last_connection_message = frappe._("Connection successful ({0}).").format(client.mode)


@frappe.whitelist()
def sync_webhooks() -> dict:
	"""Manually reconcile webhooks now (button on the settings form)."""
	from medusa_connector.medusa.services.webhook_sync import WebhookSyncService

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
