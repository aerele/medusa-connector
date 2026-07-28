# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import hmac
import json

import frappe

from medusa_connector.constants import MODULE_NAME
from medusa_connector.utils.logging import (
	create_medusa_log,
	webhook_message_key,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def receive() -> dict:
	settings = frappe.get_cached_doc("Medusa Settings")
	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)

	if not authenticate(settings):
		create_medusa_log(
			status="Error",
			method="medusa_connector.api.webhook.receive",
			message="webhook:rejected",
			request_data={
				"headers": _safe_headers(headers),
				"payload": _decode_raw(raw),
			},
			exception="Invalid or missing webhook token",
			make_new=True,
		)
		frappe.local.response["http_status_code"] = 401
		return {"status": "rejected"}

	try:
		body = json.loads(raw or b"{}")
	except (json.JSONDecodeError, TypeError):
		frappe.log_error(
			title="Medusa Webhook Invalid Payload",
			message=_decode_raw(raw),
		)
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid_payload"}

	if not isinstance(body, dict):
		frappe.log_error(
			title="Medusa Webhook Invalid Payload",
			message=json.dumps(body, indent=2, default=str),
		)
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid_payload"}

	event_name = frappe.request.args.get("event")

	if not event_name:
		frappe.log_error(
			title="Medusa Webhook Missing Event",
			message=json.dumps(
				{
					"headers": _safe_headers(headers),
					"payload": body,
				},
				indent=2,
				default=str,
			),
		)
		frappe.local.response["http_status_code"] = 400
		return {"status": "missing_event"}

	entity_id = body.get("id")

	if not entity_id:
		frappe.log_error(
			title="Medusa Webhook Missing Entity ID",
			message=json.dumps(
				{
					"event_name": event_name,
					"headers": _safe_headers(headers),
					"payload": body,
				},
				indent=2,
				default=str,
			),
		)
		frappe.local.response["http_status_code"] = 400
		return {"status": "missing_entity_id"}

	event_id = f"{event_name}:{entity_id}"

	log = create_medusa_log(
		status="Queued",
		method="medusa_connector.webhook.dispatch.dispatch_event",
		message=webhook_message_key(event_id),
		request_data={
			"event_id": event_id,
			"event_name": event_name,
			"headers": _safe_headers(headers),
			"payload": body,
			"integration": MODULE_NAME,
		},
		make_new=True,
	)

	frappe.enqueue(
		"medusa_connector.webhook.dispatch.dispatch_event",
		queue="short",
		enqueue_after_commit=True,
		log_name=log.name,
	)

	return {"status": "accepted", "log": log.name}


def authenticate(settings) -> bool:
	secret = settings.get_password("webhook_secret", raise_exception=False)
	token = frappe.request.args.get("token")

	return bool(secret and token) and hmac.compare_digest(token, secret)


def _decode_raw(raw: bytes | str) -> str:
	return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def _safe_headers(headers: dict) -> dict:
	return {str(key): str(value)[:500] for key, value in (headers or {}).items()}
