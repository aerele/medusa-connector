# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Background dispatcher — turns a logged webhook into a MedusaEvent and
routes it to the handler registered for its event name."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from medusa_connector.utils.logging import _RESULT_STATUS_MAP, create_medusa_log
from medusa_connector.webhook.base import MedusaEvent
from medusa_connector.webhook.registry import get_handler

MAX_RETRIES = 5


def dispatch_event(log_name: str | None = None, payload=None, request_id: str | None = None) -> None:
	name = log_name or request_id or frappe.flags.request_id
	if not name:
		frappe.throw(_("dispatch_event requires log_name or request_id"))
	frappe.flags.request_id = name
	log = frappe.get_doc("Ecommerce Integration Log", name)

	if log.status == "Success":
		return  # idempotency: never re-apply a successful event

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

		outcome = handler.handle(event)  # always a result()-shaped dict now
		status = _RESULT_STATUS_MAP.get(str(outcome.get("status", "success")).lower(), "Error")

		create_medusa_log(
			status=status,
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
		create_medusa_log(
			status="Error",
			message=log.message,
			exception=exc,
			response_data={"event_name": getattr(log, "message", None), "failed_at": str(now_datetime())},
			rollback=True,
			make_new=False,
		)
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
	event_name = raw.get("event_name") or ""
	event_id = raw.get("event_id") or log.name
	body = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
	if isinstance(body, dict) and "payload" in raw and "event_name" in raw:
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
