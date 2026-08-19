# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Synchronize Medusa webhook subscriptions with the ERPNext connector."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from medusa_connector.constants import MEDUSA_WEBHOOK_EVENTS
from medusa_connector.medusa.client import MedusaClient
from medusa_connector.webhook.util import receiver_base_url, strip_query

STATUS_REGISTERED = "Registered"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped"


def get_plugin_setup_instructions() -> str:
	"""Return setup instructions when the Medusa webhook plugin is missing."""
	return frappe._(
		"""<p>Install the <code>@lambdacurry/medusa-webhooks</code> plugin
		and configure the ERPNext webhook forwarder in Medusa.</p>

		<p>See the complete setup guide:
		<b>medusa_connector/docs/MEDUSA_WEBHOOK_SETUP.md</b></p>

		<p>After completing the setup and restarting Medusa,
		return to <b>ERPNext → Medusa Settings</b> and click
		<b>Sync Webhooks</b>.</p>"""
	)


class WebhookSyncService:
	"""Synchronize connector-owned webhook subscriptions in Medusa."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_single("Medusa Settings")
		self.base_endpoint = receiver_base_url()

		self._created = 0
		self._removed = 0

	def sync_webhooks(self) -> dict:
		"""Delete existing connector webhooks and recreate the required subscriptions."""
		try:
			client = MedusaClient(settings=self.settings)

			if not client.webhook_plugin_installed():
				return self._handle_missing_plugin()

			existing = client.list_webhooks() or []
			rows = self._recreate_webhooks(client, existing)

			failed = sum(1 for row in rows if row["registration_status"] == STATUS_FAILED)
			registered = sum(1 for row in rows if row["registration_status"] == STATUS_REGISTERED)

			if failed:
				status = "Error"
				message = frappe._(
					"Webhook sync completed with errors — {0} registered, {1} failed (removed {2})."
				).format(registered, failed, self._removed)
			else:
				status = "Installed"
				message = frappe._(
					"Webhook sync successful — {0} webhooks registered (removed {1}, created {2})."
				).format(registered, self._removed, self._created)

			return self._save_sync_result(status=status, message=message, rows=rows)

		except Exception as exc:
			frappe.log_error(
				frappe.get_traceback(),
				"Medusa Webhook Sync Failed",
			)

			return self._save_sync_result(
				status="Error",
				message=str(exc),
				rows=[],
			)

	def _recreate_webhooks(self, client, existing):
		rows = []

		for event in MEDUSA_WEBHOOK_EVENTS:
			rows.append(self._register_webhook(client, event))

		if any(row["registration_status"] == STATUS_FAILED for row in rows):
			return rows

		self._remove_connector_webhooks(client, existing)

		return rows

	def _remove_connector_webhooks(
		self,
		client: MedusaClient,
		existing: list[dict],
	) -> None:
		"""Delete webhooks that belong to this ERPNext connector."""
		for webhook in existing:
			if not self._is_connector_webhook(webhook):
				continue

			webhook_id = webhook.get("id")
			if not webhook_id:
				continue

			try:
				client.delete_webhook(webhook_id)
				self._removed += 1
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					"Medusa Webhook Removal Failed",
				)

	def _register_webhook(
		self,
		client: MedusaClient,
		event: str,
	) -> dict:
		"""Create one webhook registration using the common ERPNext endpoint."""
		target_url = self.base_endpoint

		try:
			response = client.create_webhook(
				event,
				target_url,
				active=True,
			)

			webhook_id = response.get("id")
			if not webhook_id:
				return self._build_registration_row(
					event,
					STATUS_FAILED,
					error=frappe._("Medusa accepted the webhook creation but returned no ID."),
				)

			self._created += 1

			return self._build_registration_row(
				event,
				STATUS_REGISTERED,
				webhook_id=webhook_id,
			)

		except Exception as exc:
			frappe.log_error(
				frappe.get_traceback(),
				"Medusa Webhook Registration Failed",
			)

			return self._build_registration_row(
				event,
				STATUS_FAILED,
				error=str(exc),
			)

	def _is_connector_webhook(self, webhook: dict) -> bool:
		"""Return True when a Medusa webhook points to this connector endpoint."""
		if not webhook:
			return False

		target_url = webhook.get("target_url") or webhook.get("targetUrl") or ""
		if not target_url:
			return False

		return strip_query(target_url).rstrip("/") == self.base_endpoint.rstrip("/")

	def _handle_missing_plugin(self) -> dict:
		"""Return a setup response when the Medusa webhook plugin is unavailable."""
		rows = [
			self._build_registration_row(
				event,
				STATUS_SKIPPED,
				error=frappe._("Webhooks plugin not installed"),
			)
			for event in MEDUSA_WEBHOOK_EVENTS
		]

		result = self._save_sync_result(
			status="Not Installed",
			message=frappe._("The @lambdacurry/medusa-webhooks plugin is not installed on Medusa."),
			rows=rows,
		)
		result["instructions"] = get_plugin_setup_instructions()

		return result

	def _build_registration_row(
		self,
		event: str,
		status: str,
		webhook_id: str | None = None,
		error: str | None = None,
	) -> dict:
		"""Build a Medusa Webhook Registration child row."""
		return {
			"medusa_event": event,
			"registration_status": status,
			"webhook_id": webhook_id,
			"erp_endpoint": self.base_endpoint,
			"last_sync_time": now_datetime(),
			"last_error": error,
		}

	def _save_sync_result(
		self,
		status: str,
		message: str,
		rows: list[dict],
	) -> dict:
		"""Persist the latest webhook synchronization result."""
		doc = frappe.get_single("Medusa Settings")

		doc.webhook_plugin_status = status
		doc.last_webhook_sync = now_datetime()
		doc.last_webhook_sync_message = message

		doc.set("webhook_subscriptions", [])

		for row in rows:
			doc.append("webhook_subscriptions", row)

		doc.save()

		return {
			"status": status,
			"message": message,
			"count": len(rows),
			"created": self._created,
			"removed": self._removed,
		}
