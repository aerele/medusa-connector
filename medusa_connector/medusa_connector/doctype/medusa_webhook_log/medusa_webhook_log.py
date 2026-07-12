# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class MedusaWebhookLog(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		error: DF.SmallText | None
		event_id: DF.Data | None
		event_name: DF.Data | None
		payload: DF.Code | None
		processed_at: DF.Datetime | None
		processing_notes: DF.SmallText | None
		request_headers: DF.Code | None
		retry_count: DF.Int
		retry_history: DF.Code | None
		signature_valid: DF.Check
		source_ip: DF.Data | None
		status: DF.Literal["Received", "Verified", "Rejected", "Queued", "Processed", "Failed"]
	# end: auto-generated types

	pass


@frappe.whitelist()
def retry_webhook(name: str) -> dict:
	"""Manually re-process a failed (or any non-processed) webhook log with the stored payload."""
	frappe.only_for("System Manager")
	return _retry_webhook(name)


@frappe.whitelist()
def bulk_retry(names: str | list) -> dict:
	"""Retry multiple webhook logs from the list view."""
	frappe.only_for("System Manager")
	if isinstance(names, str):
		names = json.loads(names)
	results = []
	for name in names or []:
		results.append(_retry_webhook(name))
	return {"results": results}


def _retry_webhook(name: str) -> dict:
	log = frappe.get_doc("Medusa Webhook Log", name)

	if log.status == "Processed":
		return {"ok": False, "message": "Already processed — skipped to preserve idempotency.", "name": name}

	if not log.payload:
		return {"ok": False, "message": "No payload stored on this log.", "name": name}

	# Append to retry history before re-queue.
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
			"previous_status": log.status,
			"previous_error": (log.error or "")[:500],
			"retry_count": log.retry_count or 0,
			"trigger": "manual",
		}
	)

	log.db_set(
		{
			"status": "Queued",
			"error": None,
			"processing_notes": f"Manual retry at {now_datetime()}",
			"retry_history": json.dumps(history, indent=2),
		},
		update_modified=True,
	)
	frappe.db.commit()

	frappe.enqueue(
		"medusa_connector.webhook.dispatch.dispatch_event",
		queue="short",
		job_id=f"medusa-webhook-manual-retry-{name}",
		deduplicate=True,
		log_name=name,
		enqueue_after_commit=True,
	)
	return {"ok": True, "message": "Re-queued for processing.", "name": name}
