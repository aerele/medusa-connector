# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Background dispatcher.

Turns a logged webhook into a :class:`MedusaEvent` and routes it to the handler
registered for its event name. Processing is idempotent (already-processed logs
are skipped) and failures are retried up to :data:`MAX_RETRIES` by a scheduler
sweep, with each attempt recorded on the log.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from medusa_connector.webhook.base import MedusaEvent
from medusa_connector.webhook.registry import get_handler

# Maximum automatic retries for a failing event before it is left as "Failed".
MAX_RETRIES = 5


def dispatch_event(log_name: str) -> None:
	"""Process one Medusa Webhook Log row. Raises on handler failure (for RQ)."""
	frappe.set_user("Administrator")
	log = frappe.get_doc("Medusa Webhook Log", log_name)

	# Idempotency: never re-apply a successfully processed event.
	if log.status == "Processed":
		return

	try:
		event = _build_event(log)
		handler = get_handler(event.name)
		if handler is None:
			# Unmapped event: acknowledge receipt without failing (extensible later).
			_finish(
				log,
				status="Processed",
				notes=f"No handler registered for '{event.name}'",
				outcome=f"No handler registered for '{event.name}'",
			)
			return

		outcome = handler.handle(event)
		_finish(log, status="Processed", notes="Handler completed successfully", outcome=outcome or None)
	except Exception:
		tb = frappe.get_traceback(with_context=True)
		_append_retry_history(log, status="Failed", error=tb, trigger="dispatch")
		log.db_set(
			{
				"status": "Failed",
				"error": tb,
				"retry_count": (log.retry_count or 0) + 1,
				"processing_notes": f"Failed at {now_datetime()}",
			},
			update_modified=True,
		)
		frappe.db.commit()
		# Re-raise so the RQ job is recorded as failed; the scheduler sweep retries.
		raise


def _finish(log, *, status: str, notes: str | None = None, outcome: str | None = None) -> None:
	log.db_set(
		{
			"status": status,
			"error": outcome,
			"processing_notes": notes,
			"processed_at": now_datetime(),
		},
		update_modified=True,
	)
	frappe.db.commit()


def _append_retry_history(log, *, status: str, error: str, trigger: str) -> None:
	history = []
	if log.retry_history:
		try:
			history = json.loads(log.retry_history)
			if not isinstance(history, list):
				history = []
		except (ValueError, TypeError):
			history = []
	history.append(
		{
			"at": str(now_datetime()),
			"status": status,
			"error": (error or "")[:1000],
			"retry_count": (log.retry_count or 0) + 1,
			"trigger": trigger,
		}
	)
	log.db_set("retry_history", json.dumps(history, indent=2), update_modified=False)


def _build_event(log) -> MedusaEvent:
	try:
		raw = json.loads(log.payload or "{}")
	except (ValueError, TypeError):
		raw = {}
	return MedusaEvent(
		name=log.event_name or "",
		event_id=log.event_id or log.name,
		data=raw.get("data") or raw,
		raw=raw,
		log_name=log.name,
	)


def retry_failed_webhooks() -> None:
	"""Scheduler sweep: re-enqueue failed events that still have retries left."""
	failed = frappe.get_all(
		"Medusa Webhook Log",
		filters={"status": "Failed", "retry_count": ["<", MAX_RETRIES]},
		pluck="name",
		limit=200,
	)
	for name in failed:
		# Reset to Queued so dispatch is allowed (status must not be Processed).
		frappe.db.set_value(
			"Medusa Webhook Log",
			name,
			{"status": "Queued", "processing_notes": f"Auto-retry scheduled at {now_datetime()}"},
			update_modified=False,
		)
		frappe.enqueue(
			"medusa_connector.webhook.dispatch.dispatch_event",
			queue="short",
			job_id=f"medusa-webhook-retry-{name}",
			deduplicate=True,
			log_name=name,
		)
