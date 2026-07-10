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
	"auto_register_webhooks",
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
		auto_register_webhooks: DF.Check
		connection_mode: DF.Literal["REST", "GraphQL"]
		connection_status: DF.Literal["Unknown", "Disconnected", "Connected", "Auth Failed", "Error"]
		enable_webhook_processing: DF.Check
		enabled: DF.Check
		graphql_url: DF.Data | None
		last_connection_message: DF.SmallText | None
		last_connection_test: DF.Datetime | None
		last_webhook_sync: DF.Datetime | None
		last_webhook_sync_message: DF.SmallText | None
		medusa_base_url: DF.Data | None
		verify_signatures: DF.Check
		webhook_plugin_status: DF.Literal["Unknown", "Installed", "Not Installed", "Error"]
		webhook_receiver_url: DF.SmallText | None
		webhook_secret: DF.Password | None
		webhook_signature_encoding: DF.Literal["base64", "hex"]
		webhook_signature_header: DF.Data | None
		webhook_subscriptions: DF.Table[MedusaWebhookRegistration]
	# end: auto-generated types

	def validate(self) -> None:
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

		try:
			client = MedusaClient(settings=self)
			# Fail fast: this probe runs synchronously inside the save request, so a
			# slow/unreachable Medusa must not stall the form for the full 30s.
			client.timeout = 10
			client.health_check()
		except (MedusaAuthError, MedusaConnectionError) as exc:
			frappe.throw(
				frappe._("Cannot enable Medusa Connector — connection test failed: {0}").format(str(exc)),
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
