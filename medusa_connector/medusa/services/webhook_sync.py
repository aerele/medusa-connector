# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Webhook registration sync service.

Reconciles the events the connector knows how to handle (the registry) with the
webhook subscriptions actually present in Medusa: it registers what's missing,
re-points what drifted (e.g. the ERP URL changed), removes duplicates and
orphans, and mirrors the whole picture into the Medusa Settings child table.

The service is transport-agnostic and side-effect-isolated: it owns exactly one
save of the settings doc and never talks to the receiver, so it stays unit-focused
and safe to call from a save hook, a button, or the scheduler.
"""

import frappe
from frappe.utils import now_datetime

from medusa_connector.medusa.client import MedusaClient
from medusa_connector.medusa.exceptions import MedusaConnectorError
from medusa_connector.webhook.registry import registered_events
from medusa_connector.webhook.util import receiver_base_url, signed_target_url, strip_query

# Status values mirror the Medusa Webhook Registration child doctype options.
STATUS_REGISTERED = "Registered"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped"
STATUS_REMOVED = "Removed"

# Bounded timeout for the interactive/scheduled sync so the button or worker is
# never stalled for long on a slow/unreachable Medusa.
SYNC_TIMEOUT = 15

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
<li>Back here, keep <b>Auto Register Webhooks</b> on and click <b>Sync Webhooks</b> —
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
		self.auto = bool(self.settings.auto_register_webhooks)

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
			client.timeout = SYNC_TIMEOUT
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
		registered = sum(1 for r in rows if r["registration_status"] == STATUS_REGISTERED)
		return self._finish(
			status="Installed",
			message=frappe._("Webhooks synchronised — {0} registered.").format(registered),
			rows=rows,
		)

	# -- reconciliation ------------------------------------------------
	def _reconcile(self, client: MedusaClient, existing: list[dict]) -> list[dict]:
		target_url = signed_target_url(self.secret)
		ours = [w for w in existing if self._is_ours(w)]
		desired = set(registered_events())
		rows: list[dict] = []

		for event in registered_events():
			matches = [w for w in ours if w.get("event_type") == event]
			try:
				webhook_id = self._ensure_event(client, event, matches, target_url)
			except MedusaConnectorError as exc:
				rows.append(self._row(event, STATUS_FAILED, error=str(exc)))
				continue

			if webhook_id is None:
				rows.append(self._row(event, STATUS_SKIPPED, error="Auto Register Webhooks is off"))
			else:
				rows.append(self._row(event, STATUS_REGISTERED, webhook_id=webhook_id))

		# Remove subscriptions we own for events we no longer handle.
		for w in ours:
			if w.get("event_type") not in desired and self.auto:
				try:
					client.delete_webhook(w["id"])
					rows.append(self._row(w.get("event_type"), STATUS_REMOVED, webhook_id=w.get("id")))
				except MedusaConnectorError as exc:
					rows.append(
						self._row(w.get("event_type"), STATUS_FAILED, webhook_id=w.get("id"), error=str(exc))
					)

		return rows

	def _ensure_event(self, client, event: str, matches: list[dict], target_url: str) -> str | None:
		"""Guarantee exactly one correct subscription for ``event``. Returns its id.

		Returns ``None`` when nothing exists and auto-registration is off.
		"""
		# De-duplicate: keep the first, drop the rest.
		keep = matches[0] if matches else None
		for dup in matches[1:]:
			if self.auto:
				client.delete_webhook(dup["id"])

		if keep is None:
			if not self.auto:
				return None
			created = client.create_webhook(event, target_url)
			return created.get("id")

		# Re-point if the stored URL drifted (site URL or secret changed).
		if self.auto and keep.get("target_url") != target_url:
			client.delete_webhook(keep["id"])
			created = client.create_webhook(event, target_url)
			return created.get("id")

		return keep.get("id")

	def _is_ours(self, webhook: dict) -> bool:
		"""A subscription is ours when its endpoint (query stripped) is our receiver."""
		return strip_query(webhook.get("target_url", "")) == strip_query(self.base_endpoint)

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
		return {"status": status, "message": message, "count": len(rows)}

	@staticmethod
	def _replace_child_rows(parent: str, rows: list[dict]) -> None:
		"""Swap the ``webhook_subscriptions`` grid rows without touching the parent."""
		frappe.db.delete(
			"Medusa Webhook Registration",
			{"parenttype": parent, "parentfield": "webhook_subscriptions"},
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
