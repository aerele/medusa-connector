# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Webhook registration sync service.

Reconciles the events the connector knows how to handle (the registry) with the
webhook subscriptions actually present in Medusa. Triggered by the "Sync
Webhooks" button and by a scheduled (cron) job — there is no background job
queued on save.

"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from medusa_connector.medusa.client import MedusaClient
from medusa_connector.medusa.exceptions import MedusaConnectorError
from medusa_connector.webhook.registry import registered_events
from medusa_connector.webhook.util import (
	receiver_base_url,
	signed_target_url,
	strip_query,
)

# Status values mirror the Medusa Webhook Registration child doctype options.
STATUS_REGISTERED = "Registered"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped"
STATUS_REMOVED = "Removed"

# Single-flight lock so overlapping syncs (button vs. cron) cannot create
# duplicate registrations.
SYNC_LOCK_KEY = "medusa_connector:webhook_sync_lock"
SYNC_LOCK_TTL = 180


def plugin_setup_steps() -> str:
	"""HTML install guide shown when the Medusa webhooks plugin is missing.

	Single source of truth, also mirrored in docs/MEDUSA_WEBHOOK_SETUP.md.
	"""
	endpoint = receiver_base_url()
	return frappe._(
		"""<p>Medusa v2 has no built-in outbound webhooks. Install the supported plugin on
your Medusa server (one-time), then click <b>Sync Webhooks</b> again.</p>
<ol>
<li>In your Medusa project: <code>npm install @lambdacurry/medusa-webhooks</code></li>
<li>Add it to <code>medusa-config.ts</code> under <code>plugins</code>:
<pre>plugins: [{{ resolve: "@lambdacurry/medusa-webhooks", options: {{}} }}]</pre></li>
<li>Run migrations: <code>npx medusa db:migrate</code></li>
<li>Restart Medusa.</li>
<li>Back here, click <b>Sync Webhooks</b> —
the connector registers every required event to your ERP endpoint automatically:<br>
<code>{endpoint}</code></li>
</ol>
<p>Full guide: <code>apps/medusa_connector/docs/MEDUSA_WEBHOOK_SETUP.md</code></p>"""
	).format(endpoint=endpoint)


class WebhookSyncService:
	"""Idempotent reconciler between the event registry and Medusa's webhooks."""

	def __init__(self, settings=None) -> None:
		# Operate on a real (savable) doc, freshly loaded by default.
		self.settings = settings or frappe.get_single("Medusa Settings")
		self.base_endpoint = receiver_base_url()
		self.secret = self.settings.get_password("webhook_secret", raise_exception=False)
		# Ids previously mirrored into the settings child table — used as a
		# secondary ownership signal when the site URL has drifted.
		self._known_ids = {
			row.webhook_id
			for row in (self.settings.webhook_subscriptions or [])
			if getattr(row, "webhook_id", None)
		}
		# Ids we delete during this pass (dups / repair) so orphan cleanup skips them.
		self._deleted_ids: set[str] = set()
		# Action counters for the operator-facing summary message.
		self._created = 0
		self._updated = 0
		self._removed = 0

	# -- public API ----------------------------------------------------
	def sync(self) -> dict:
		"""Run one reconciliation pass under a single-flight lock."""
		cache = frappe.cache()
		if not cache.set(SYNC_LOCK_KEY, "1", nx=True, ex=SYNC_LOCK_TTL):
			return {
				"status": "Busy",
				"message": frappe._("A webhook sync is already running. Wait a moment and refresh."),
				"count": 0,
			}
		try:
			return self._run()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Medusa Webhook Sync Failed")
			return {
				"status": "Error",
				"message": frappe._("Webhook sync failed unexpectedly. Check the Error Log for details."),
				"count": 0,
			}
		finally:
			cache.delete(SYNC_LOCK_KEY)

	def _run(self) -> dict:
		try:
			client = MedusaClient(settings=self.settings)
			installed = client.webhook_plugin_installed()
			existing = client.list_webhooks() if installed else []
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "Medusa Webhook Sync Failed")
			result = self._finish(status="Error", message=str(exc), rows=[])
			result["instructions"] = frappe._(
				"Could not reach Medusa or authenticate. Check the Base URL, Admin API Key, "
				"and that Medusa is running, then try again."
			)
			return result

		if not installed:
			rows = [
				self._row(ev, STATUS_SKIPPED, error=frappe._("Webhooks plugin not installed"))
				for ev in registered_events()
			]
			result = self._finish(
				status="Not Installed",
				message=frappe._("The @lambdacurry/medusa-webhooks plugin is not installed on Medusa."),
				rows=rows,
			)
			result["instructions"] = plugin_setup_steps()
			return result

		try:
			rows = self._reconcile(client, existing)
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "Medusa Webhook Sync Failed")
			return self._finish(status="Error", message=str(exc), rows=[])

		registered = sum(1 for r in rows if r["registration_status"] == STATUS_REGISTERED)
		failed = sum(1 for r in rows if r["registration_status"] == STATUS_FAILED)

		if failed:
			message = frappe._(
				"Sync completed with errors — {0} registered, {1} failed "
				"(created {2}, updated {3}, removed {4})."
			).format(registered, failed, self._created, self._updated, self._removed)
		else:
			message = frappe._(
				"Sync successful — {0} webhooks registered (created {1}, updated {2}, removed {3})."
			).format(registered, self._created, self._updated, self._removed)

		return self._finish(status="Installed", message=message, rows=rows)

	# -- reconciliation ------------------------------------------------
	def _reconcile(self, client: MedusaClient, existing: list[dict]) -> list[dict]:
		"""Identify connector webhooks, clean up old/duplicate ones, then register."""
		ours = [w for w in existing if self._is_ours(w)]
		desired_events = registered_events()
		desired = set(desired_events)

		by_event: dict[str, list[dict]] = {}
		for w in ours:
			event_type = w.get("event_type") or w.get("eventType")
			by_event.setdefault(event_type, []).append(w)

		rows: list[dict] = []

		# -- clean up: orphaned events and duplicates --
		for event_type, webhooks in list(by_event.items()):
			if event_type not in desired:
				for w in webhooks:
					wid = w.get("id")
					if not wid:
						continue
					try:
						client.delete_webhook(wid)
						self._deleted_ids.add(wid)
						self._removed += 1
						rows.append(self._row(event_type, STATUS_REMOVED, webhook_id=wid))
					except Exception as exc:
						frappe.log_error(frappe.get_traceback(), "Medusa Webhook Cleanup Failed")
						rows.append(self._row(event_type, STATUS_FAILED, webhook_id=wid, error=str(exc)))
				continue

			if len(webhooks) > 1:
				target_url = signed_target_url(self.secret, event_type)
				correct = [w for w in webhooks if self._is_correct(w, event_type, target_url)]
				keep = correct[0] if correct else webhooks[0]
				for dup in webhooks:
					if dup is keep:
						continue
					dup_id = dup.get("id")
					if not dup_id:
						continue
					try:
						client.delete_webhook(dup_id)
						self._deleted_ids.add(dup_id)
						self._removed += 1
					except Exception:
						frappe.log_error(frappe.get_traceback(), "Medusa Webhook Cleanup Failed")
				by_event[event_type] = [keep]

		# -- register: ensure exactly one correct subscription per required event --
		for event in desired_events:
			matches = by_event.get(event, [])
			target_url = signed_target_url(self.secret, event)
			try:
				webhook_id = self._ensure_event(client, event, matches, target_url)
			except Exception as exc:
				frappe.log_error(frappe.get_traceback(), "Medusa Webhook Registration Failed")
				rows.append(self._row(event, STATUS_FAILED, error=str(exc)))
				continue

			if webhook_id is None:
				rows.append(
					self._row(event, STATUS_FAILED, error=frappe._("Could not ensure webhook registration"))
				)
			else:
				rows.append(self._row(event, STATUS_REGISTERED, webhook_id=webhook_id))

		return rows

	def _ensure_event(
		self,
		client: MedusaClient,
		event: str,
		matches: list[dict],
		target_url: str,
	) -> str | None:
		"""Guarantee exactly one correct subscription for ``event``. Returns its id.

		``matches`` should already contain at most one webhook (duplicates are
		collapsed earlier in ``_reconcile``), but this still tolerates a stray
		extra defensively.
		"""
		keep = matches[0] if matches else None

		if keep is None:
			created = client.create_webhook(event, target_url, active=True)
			webhook_id = created.get("id")
			if not webhook_id:
				raise MedusaConnectorError(
					frappe._("Medusa accepted the webhook create but returned no id for {0}.").format(event)
				)
			self._created += 1
			return webhook_id

		if self._is_correct(keep, event, target_url):
			return keep.get("id")

		# Drifted configuration: try in-place update, fall back to recreate.
		keep_id = keep.get("id")
		try:
			updated = client.update_webhook(keep_id, event, target_url, active=True)
			self._updated += 1
			return updated.get("id") or keep_id
		except MedusaConnectorError:
			client.delete_webhook(keep_id)
			self._deleted_ids.add(keep_id)
			self._removed += 1
			created = client.create_webhook(event, target_url, active=True)
			webhook_id = created.get("id")
			if not webhook_id:
				raise MedusaConnectorError(
					frappe._("Failed to recreate webhook for {0} after update failure.").format(event)
				)
			self._created += 1
			return webhook_id

	def _is_ours(self, webhook: dict) -> bool:
		"""Return True only for webhooks owned by this ERPNext site."""
		if not webhook:
			return False

		# Strong ownership signal:
		# This webhook was previously registered and stored by this site.
		webhook_id = webhook.get("id")
		if webhook_id and webhook_id in self._known_ids:
			return True

		target = webhook.get("target_url") or webhook.get("targetUrl") or ""
		if not target:
			return False
		return strip_query(target).rstrip("/") == self.base_endpoint.rstrip("/")

	@staticmethod
	def _is_correct(webhook: dict, event: str, target_url: str) -> bool:
		"""Return True when the webhook exactly matches the desired configuration."""
		event_type = webhook.get("event_type") or webhook.get("eventType")
		stored_url = webhook.get("target_url") or webhook.get("targetUrl") or ""
		active = webhook.get("active")

		# Older plugin payloads may not return active.
		if active is None:
			active = True

		return event_type == event and stored_url == target_url and bool(active)

	# -- persistence ---------------------------------------------------
	def _row(self, event, status, webhook_id=None, error=None) -> dict:
		return {
			"medusa_event": event,
			"registration_status": status,
			"webhook_id": webhook_id,
			"erp_endpoint": self.base_endpoint,
			"last_sync_time": now_datetime(),
			"last_error": error,
		}

	def _finish(self, status: str, message: str, rows: list[dict]) -> dict:
		"""Persist the sync result on a fresh copy of Medusa Settings."""
		doc = frappe.get_doc("Medusa Settings")
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
			"updated": self._updated,
			"removed": self._removed,
		}
