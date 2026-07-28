# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import now_datetime

from medusa_connector.constants import _RESULT_STATUS_MAP
from medusa_connector.utils.logging import create_medusa_log
from medusa_connector.webhook.registry import get_handler


@dataclass
class MedusaEvent:
	name: str
	event_id: str
	data: dict
	log_name: str | None = None

	@property
	def entity_id(self) -> str | None:
		return self.data.get("id")


def dispatch_event(
	log_name: str | None = None,
	request_id: str | None = None,
	payload: dict | None = None,
) -> None:
	log_name = log_name or request_id

	if not log_name:
		frappe.throw(_("dispatch_event requires log_name"))

	# Webhook jobs run in the background without the request user's context.
	# Use Administrator for the integration flow and avoid ignore_permissions.
	frappe.set_user("Administrator")

	frappe.flags.request_id = log_name
	log = frappe.get_doc("Ecommerce Integration Log", log_name)

	if log.status == "Success":
		return

	try:
		event = _build_event(log, payload)

		if not event.name:
			raise ValueError(f"Webhook event name is missing for log {log.name}")

		handler = get_handler(event.name)

		if handler is None:
			create_medusa_log(
				status="Skipped",
				message=log.message,
				response_data={
					"event_name": event.name,
					"event_id": event.event_id,
					"outcome": f"No handler registered for '{event.name}'",
					"processed_at": str(now_datetime()),
				},
				make_new=False,
			)
			return

		outcome = handler.handle(event)

		if not isinstance(outcome, dict):
			raise TypeError(
				f"{handler.__class__.__name__}.handle() must return a dict, got {type(outcome).__name__}"
			)

		status = _RESULT_STATUS_MAP.get(
			str(outcome.get("status", "error")).lower(),
			"Error",
		)

		create_medusa_log(
			status=status,
			message=log.message,
			response_data={
				"event_name": event.name,
				"event_id": event.event_id,
				"outcome": outcome,
				"processed_at": str(now_datetime()),
			},
			make_new=False,
		)

	except Exception as exc:
		_log_dispatch_error(log, exc)
		raise


def _build_event(log, payload=None) -> MedusaEvent:
	if payload is None:
		try:
			request_data = json.loads(log.request_data or "{}")
		except (json.JSONDecodeError, TypeError):
			request_data = {}
	else:
		request_data = payload

	if not isinstance(request_data, dict):
		request_data = {}

	webhook_payload = request_data.get("payload")

	if not isinstance(webhook_payload, dict):
		webhook_payload = {}

	return MedusaEvent(
		name=str(request_data.get("event_name") or ""),
		event_id=str(request_data.get("event_id") or log.name),
		data=webhook_payload,
		log_name=log.name,
	)


def _log_dispatch_error(log, exc: Exception) -> None:
	try:
		create_medusa_log(
			status="Error",
			message=log.message,
			exception=exc,
			response_data={
				"event_name": _get_event_name_from_log(log),
				"event_id": _get_event_id_from_log(log),
				"failed_at": str(now_datetime()),
			},
			rollback=True,
			make_new=False,
		)
	except Exception:
		frappe.logger("medusa_connector").error(
			"Failed to update webhook integration log",
			exc_info=True,
		)


def _get_event_name_from_log(log) -> str:
	try:
		request_data = json.loads(log.request_data or "{}")
	except (json.JSONDecodeError, TypeError):
		return ""

	if not isinstance(request_data, dict):
		return ""

	return str(request_data.get("event_name") or "")


def _get_event_id_from_log(log) -> str:
	try:
		request_data = json.loads(log.request_data or "{}")
	except (json.JSONDecodeError, TypeError):
		return ""

	if not isinstance(request_data, dict):
		return ""

	return str(request_data.get("event_id") or "")
