# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Webhook registration sync service.

Reconciles the events the connector knows how to handle (the registry) with the
webhook subscriptions actually present in Medusa:

1. Fetch every subscription from Medusa.
2. Identify only those owned by this connector (receiver path / known ids).
3. Create missing required events.
4. Verify callback URL, event, secret (token query), and active flag; update or
   recreate when configuration has drifted.
5. Remove duplicate connector-owned registrations for the same event.
6. Remove connector-owned subscriptions for events the connector no longer
   handles.
7. Never create, update, or delete webhooks that belong to other integrations.

The operation is idempotent: repeated Sync Webhooks runs leave the connector's
webhooks correctly configured without inventing duplicates.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import frappe
from frappe.utils import now_datetime

from medusa_connector.medusa.client import MedusaClient
from medusa_connector.medusa.exceptions import MedusaConnectorError
from medusa_connector.webhook.registry import registered_events
from medusa_connector.webhook.util import (
	RECEIVER_METHOD,
	receiver_base_url,
	signed_target_url,
	strip_query,
)

# Status values mirror the Medusa Webhook Registration child doctype options.
STATUS_REGISTERED = "Registered"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped"
STATUS_REMOVED = "Removed"

# Single-flight lock so overlapping syncs cannot create duplicate registrations.
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
		"""Run one reconciliation pass under a single-flight lock.

		The button, the on-save background job, and the scheduler can all trigger a
		sync. Without a lock, two overlapping runs each see "no webhooks yet" and
		both register every event → duplicates. The Redis lock serialises them: a
		second caller returns ``Busy`` instead of racing.
		"""
		cache = frappe.cache()
		if not cache.set(SYNC_LOCK_KEY, "1", nx=True, ex=SYNC_LOCK_TTL):
			return {
				"status": "Busy",
				"message": frappe._("A webhook sync is already running. Wait a moment and refresh."),
				"count": 0,
			}
		try:
			return self._run()
		finally:
			cache.delete(SYNC_LOCK_KEY)

	def _run(self) -> dict:
		"""Reconciliation pass (assumes the sync lock is held)."""
		# Any connectivity/auth/config failure while talking to Medusa is recorded
		# as an Error state rather than raised — the sync must never crash a save,
		# a scheduler tick, or the button.
		try:
			client = MedusaClient(settings=self.settings)
			installed = client.webhook_plugin_installed()
			existing = client.list_webhooks() if installed else []
		except MedusaConnectorError as exc:
			result = self._finish(status="Error", message=str(exc), rows=[])
			result["instructions"] = frappe._(
				"Could not reach Medusa or authenticate. Check the Base URL, Admin API Key, "
				"and that Medusa is running, then try again."
			)
			return result

		if not installed:
			rows = [
				self._row(ev, STATUS_SKIPPED, error="Webhooks plugin not installed")
				for ev in registered_events()
			]
			result = self._finish(
				status="Not Installed",
				message="The @lambdacurry/medusa-webhooks plugin is not installed on Medusa.",
				rows=rows,
			)
			result["instructions"] = plugin_setup_steps()
			return result

		rows = self._reconcile(client, existing)

		# Second pass: re-fetch Medusa and heal anything still missing/wrong so a
		# partial failure on the first pass cannot leave the registry half-applied.
		try:
			rows = self._verify_and_heal(client, rows)
		except MedusaConnectorError as exc:
			# Keep first-pass rows but surface the verification failure.
			for row in rows:
				if row["registration_status"] == STATUS_REGISTERED and not row.get("last_error"):
					row["last_error"] = frappe._("Post-sync verification failed: {0}").format(str(exc))

		registered = sum(1 for r in rows if r["registration_status"] == STATUS_REGISTERED)
		message = frappe._(
			"Webhooks synchronised — {0} registered (created {1}, updated {2}, removed {3})."
		).format(registered, self._created, self._updated, self._removed)
		return self._finish(status="Installed", message=message, rows=rows)

	# -- reconciliation ------------------------------------------------
	def _reconcile(self, client: MedusaClient, existing: list[dict]) -> list[dict]:
		ours = [w for w in existing if self._is_ours(w)]
		desired = set(registered_events())
		rows: list[dict] = []

		for event in registered_events():
			matches = [w for w in ours if (w.get("event_type") or w.get("eventType")) == event]
			target_url = signed_target_url(self.secret, event)
			try:
				webhook_id = self._ensure_event(client, event, matches, target_url)
			except MedusaConnectorError as exc:
				rows.append(self._row(event, STATUS_FAILED, error=str(exc)))
				continue

			if webhook_id is None:
				rows.append(self._row(event, STATUS_FAILED, error="Could not ensure webhook registration"))
			else:
				rows.append(self._row(event, STATUS_REGISTERED, webhook_id=webhook_id))

		# Remove subscriptions we own for events we no longer handle.
		# Duplicates deleted inside ``_ensure_event`` are skipped via ``_deleted_ids``.
		for w in ours:
			wid = w.get("id")
			if not wid or wid in self._deleted_ids:
				continue
			event_type = w.get("event_type") or w.get("eventType")
			if event_type in desired:
				continue
			try:
				client.delete_webhook(wid)
				self._deleted_ids.add(wid)
				self._removed += 1
				rows.append(self._row(event_type, STATUS_REMOVED, webhook_id=wid))
			except MedusaConnectorError as exc:
				rows.append(self._row(event_type, STATUS_FAILED, webhook_id=wid, error=str(exc)))

		return rows

	def _ensure_event(self, client, event: str, matches: list[dict], target_url: str) -> str | None:
		"""Guarantee exactly one correct subscription for ``event``. Returns its id."""
		# Prefer a subscription that already matches the desired configuration.
		correct = [w for w in matches if self._is_correct(w, event, target_url)]
		keep = correct[0] if correct else (matches[0] if matches else None)
		dups = [w for w in matches if w is not keep]

		for dup in dups:
			dup_id = dup.get("id")
			if not dup_id or dup_id in self._deleted_ids:
				continue
			client.delete_webhook(dup_id)
			self._deleted_ids.add(dup_id)
			self._removed += 1

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

	def _verify_and_heal(self, client: MedusaClient, rows: list[dict]) -> list[dict]:
		"""Re-list Medusa and repair any still-missing or still-wrong connector webhooks."""
		existing = client.list_webhooks()
		ours = [w for w in existing if self._is_ours(w)]
		by_event: dict[str, list[dict]] = {}
		for w in ours:
			event_type = w.get("event_type") or w.get("eventType")
			if not event_type:
				continue
			by_event.setdefault(event_type, []).append(w)

		healed: list[dict] = []
		for row in rows:
			event = row.get("medusa_event")
			if row.get("registration_status") not in (STATUS_REGISTERED, STATUS_FAILED):
				healed.append(row)
				continue
			if not event or event not in set(registered_events()):
				healed.append(row)
				continue

			target_url = signed_target_url(self.secret, event)
			matches = by_event.get(event, [])
			correct = [w for w in matches if self._is_correct(w, event, target_url)]

			if len(correct) == 1 and len(matches) == 1:
				healed.append(self._row(event, STATUS_REGISTERED, webhook_id=correct[0].get("id")))
				continue

			# Missing, duplicate, or still wrong — run ensure again against live state.
			try:
				webhook_id = self._ensure_event(client, event, matches, target_url)
				healed.append(self._row(event, STATUS_REGISTERED, webhook_id=webhook_id))
			except MedusaConnectorError as exc:
				healed.append(
					self._row(
						event,
						STATUS_FAILED,
						webhook_id=row.get("webhook_id"),
						error=str(exc),
					)
				)

		# Preserve Removed rows from the first pass (not re-derived from the re-list).
		removed = [r for r in rows if r.get("registration_status") == STATUS_REMOVED]
		# De-dupe by event: prefer healed/registered rows over removed for same event.
		healed_events = {r.get("medusa_event") for r in healed}
		for r in removed:
			if r.get("medusa_event") not in healed_events:
				healed.append(r)

		return healed

	def _is_ours(self, webhook: dict) -> bool:
		"""Return True only for subscriptions managed by this connector.

		Ownership signals (any one is enough):
		- callback path is our unique receiver method
		- full base endpoint matches the current site receiver URL
		- id was previously recorded in Medusa Settings (site URL may have changed)
		"""
		if not webhook:
			return False

		wid = webhook.get("id")
		if wid and wid in self._known_ids:
			return True

		target = webhook.get("target_url") or webhook.get("targetUrl") or ""
		if not target:
			return False

		path = urlsplit(target).path.rstrip("/")
		receiver_path = RECEIVER_METHOD.rstrip("/")
		if path == receiver_path or path.endswith(receiver_path):
			return True

		return strip_query(target) == strip_query(self.base_endpoint)

	@staticmethod
	def _is_correct(webhook: dict, event: str, target_url: str) -> bool:
		"""True when event, callback URL (incl. secret/token), and active match."""
		event_type = webhook.get("event_type") or webhook.get("eventType")
		stored_url = webhook.get("target_url") or webhook.get("targetUrl") or ""
		active = webhook.get("active")
		# Treat missing ``active`` as True for older plugin payloads.
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
		"""Persist results WITHOUT bumping the parent's ``modified``.

		This runs from a background job/scheduler while an operator may have the
		Settings form open. A normal ``doc.save()`` would advance ``modified`` and
		make their next save fail with a TimestampMismatchError, so status fields
		are written via ``set_value(update_modified=False)`` and the child table is
		rewritten directly — the parent timestamp (and the operator's form) is left
		untouched.
		"""
		parent = "Medusa Settings"
		frappe.db.set_value(
			parent,
			parent,
			{
				"webhook_plugin_status": status,
				"last_webhook_sync": now_datetime(),
				"last_webhook_sync_message": message,
			},
			update_modified=False,
		)
		self._replace_child_rows(parent, rows)
		frappe.clear_document_cache(parent, parent)
		frappe.db.commit()
		return {
			"status": status,
			"message": message,
			"count": len(rows),
			"created": self._created,
			"updated": self._updated,
			"removed": self._removed,
		}

	@staticmethod
	def _replace_child_rows(parent: str, rows: list[dict]) -> None:
		"""Replace the webhook registration child table without updating the parent."""
		frappe.db.delete(
			"Medusa Webhook Registration",
			{"parent": parent, "parenttype": parent, "parentfield": "webhook_subscriptions"},
		)

		for idx, row in enumerate(rows, start=1):
			child = frappe.get_doc(
				{
					"doctype": "Medusa Webhook Registration",
					"parent": parent,
					"parenttype": parent,
					"parentfield": "webhook_subscriptions",
					"idx": idx,
					**row,
				}
			)
			child.name = frappe.generate_hash(length=10)
			child.db_insert()


def sync_webhooks() -> dict:
	"""Module-level entry point used by the button and save hook."""
	return WebhookSyncService().sync()


def scheduled_webhook_sync() -> None:
	"""Hourly scheduler hook: heal webhook drift while the connector is enabled."""
	if not frappe.db.get_single_value("Medusa Settings", "enabled"):
		return
	WebhookSyncService().sync()
