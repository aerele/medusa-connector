# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Background dispatcher.

Turns a logged webhook into a :class:`MedusaEvent` and routes it to the handler
registered for its event name. Processing is idempotent (already-processed logs
are skipped) and failures are retried up to :data:`MAX_RETRIES` by a scheduler
sweep, with each attempt recorded on the log.
"""

import json

import frappe
from frappe.utils import now_datetime

from medusa_connector.webhook.handlers.base import MedusaEvent
from medusa_connector.webhook.registry import get_handler

# Maximum automatic retries for a failing event before it is left as "Failed".
MAX_RETRIES = 5


def dispatch_event(log_name: str) -> None:
	"""Process one Medusa Webhook Log row. Raises on handler failure (for RQ)."""
	log = frappe.get_doc("Medusa Webhook Log", log_name)

	# Idempotency: never re-apply a successfully processed event.
	if log.status == "Processed":
		return

	try:
		event = _build_event(log)
		handler = get_handler(event.name)
		if handler is None:
			# Unmapped event: acknowledge receipt without failing (extensible later).
			log.db_set("status", "Processed", commit=False)
			log.db_set("error", f"No handler registered for '{event.name}'", commit=False)
			log.db_set("processed_at", now_datetime(), commit=True)
			return

		outcome = handler.handle(event)
		log.db_set("status", "Processed", commit=False)
		log.db_set("error", outcome or None, commit=False)
		log.db_set("processed_at", now_datetime(), commit=True)
	except Exception:
		log.db_set("status", "Failed", commit=False)
		log.db_set("error", frappe.get_traceback(with_context=True), commit=False)
		log.db_set("retry_count", (log.retry_count or 0) + 1, commit=True)
		# Re-raise so the RQ job is recorded as failed; the scheduler sweep retries.
		raise


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
		frappe.enqueue(
			"medusa_connector.webhook.dispatch.dispatch_event",
			queue="short",
			job_id=f"medusa-webhook-retry-{name}",
			deduplicate=True,
			log_name=name,
		)
