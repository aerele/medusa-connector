# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Background dispatcher.

Turns a logged webhook (Ecommerce Integration Log) into a :class:`MedusaEvent`
and routes it to the handler registered for its event name.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from medusa_connector.constants import MODULE_NAME
from medusa_connector.utils.logging import create_medusa_log
from medusa_connector.webhook.base import MedusaEvent
from medusa_connector.webhook.registry import get_handler

# Maximum automatic retries for a failing event before it is left as Error.
MAX_RETRIES = 5


def dispatch_event(log_name: str | None = None, payload=None, request_id: str | None = None) -> None:
	"""Process one Ecommerce Integration Log webhook row.
	Accepts ``log_name`` or standard ``request_id`` / ``payload``.
	"""
	name = log_name or request_id or frappe.flags.request_id
	if not name:
		frappe.throw(_("dispatch_event requires log_name or request_id"))
	frappe.flags.request_id = name
	log = frappe.get_doc("Ecommerce Integration Log", name)

	# Idempotency: never re-apply a successful event.
	if log.status == "Success":
		return

	try:
		event = _build_event(log, payload)
		handler = get_handler(event.name)
		if handler is None:
			create_medusa_log(
				status="Success",
				message=log.message,
				response_data={"outcome": f"No handler registered for '{event.name}'"},
				make_new=False,
			)
			return

		outcome = handler.handle(event)
		create_medusa_log(
			status="Success",
			message=log.message,
			response_data={
				"outcome": outcome,
				"event_name": event.name,
				"event_id": event.event_id,
				"processed_at": str(now_datetime()),
			},
			make_new=False,
		)
	except Exception as exc:
		# create_log with rollback=True rolls back then re-inserts log update.
		create_medusa_log(
			status="Error",
			message=log.message,
			exception=exc,
			response_data={
				"event_name": getattr(log, "message", None),
				"failed_at": str(now_datetime()),
			},
			rollback=True,
			make_new=False,
		)
		# Restore request_id after rollback path
		frappe.flags.request_id = name
		raise


def _build_event(log, payload=None) -> MedusaEvent:
	raw = payload
	if raw is None:
		try:
			raw = json.loads(log.request_data or "{}")
		except (ValueError, TypeError):
			raw = {}

	if not isinstance(raw, dict):
		raw = {"data": raw}

	# Support both structured wrapper and bare body.
	event_name = raw.get("event_name") or ""
	event_id = raw.get("event_id") or log.name
	body = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
	if isinstance(body, dict) and "payload" in raw and "event_name" in raw:
		# wrapper from receive()
		pass
	elif isinstance(raw.get("data"), dict):
		body = raw

	if not event_name and isinstance(body, dict):
		event_name = body.get("event") or body.get("name") or body.get("event_name") or ""

	data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
	if not isinstance(data, dict):
		data = {}

	return MedusaEvent(
		name=event_name or "",
		event_id=str(event_id),
		data=data,
		raw=body if isinstance(body, dict) else raw,
		log_name=log.name,
	)


def retry_failed_webhooks() -> None:
	"""Scheduler sweep: re-enqueue failed Medusa webhook jobs still under the retry cap.

	Uses response_data.failed_at / message prefix webhook: and status Error.
	"""
	failed = frappe.get_all(
		"Ecommerce Integration Log",
		filters={
			"integration": MODULE_NAME,
			"status": "Error",
			"method": ["like", "%dispatch_event%"],
		},
		fields=["name", "message", "response_data"],
		limit=200,
		order_by="modified asc",
	)
	for row in failed:
		# Cap retries by counting Error updates is not stored; use response_data if present.
		retry_count = 0
		try:
			resp = json.loads(row.response_data or "{}")
			retry_count = int(resp.get("retry_count") or 0)
		except (ValueError, TypeError):
			retry_count = 0
		if retry_count >= MAX_RETRIES:
			continue

		frappe.db.set_value(
			"Ecommerce Integration Log",
			row.name,
			{
				"status": "Queued",
				"traceback": "",
			},
			update_modified=False,
		)
		# Bump retry counter in response_data
		try:
			resp = json.loads(row.response_data or "{}")
		except (ValueError, TypeError):
			resp = {}
		resp["retry_count"] = retry_count + 1
		resp["retry_scheduled_at"] = str(now_datetime())
		frappe.db.set_value(
			"Ecommerce Integration Log",
			row.name,
			"response_data",
			json.dumps(resp, indent=2),
			update_modified=False,
		)

		frappe.enqueue(
			"medusa_connector.webhook.dispatch.dispatch_event",
			queue="short",
			job_id=f"medusa-webhook-retry-{row.name}",
			deduplicate=True,
			log_name=row.name,
			request_id=row.name,
		)
