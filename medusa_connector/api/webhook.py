# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import hashlib
import hmac
import json

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from medusa_connector.constants import MODULE_NAME
from medusa_connector.utils.logging import (
	create_medusa_log,
	redact,
	webhook_message_key,
)

DEDUPE_WINDOW_SECONDS = 3600
MAX_TIMESTAMP_SKEW = 300  # 5 minutes


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=60, seconds=60)
def receive() -> dict:
	settings = frappe.get_cached_doc("Medusa Settings")
	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)

	if not authenticate(settings, raw, headers):
		create_medusa_log(
			status="Error",
			method="medusa_connector.api.webhook.receive",
			message="webhook:rejected",
			request_data={
				"headers": _safe_headers(headers),
				"payload": _redact_raw(raw),
			},
			exception="Invalid webhook signature",
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

	# Medusa sends:
	# {
	#     "event": "product.created",
	#     "data": {
	#         "id": "prod_...",
	#         ...
	#     }
	# }
	data = body.get("data")

	if not isinstance(data, dict):
		data = {}

	event_name = frappe.request.headers.get("X-Medusa-Event")
	event_id = frappe.request.headers.get("X-Medusa-Event-Id")

	if not event_name:
		frappe.local.response["http_status_code"] = 400
		return {"status": "missing_event"}

	entity_id = data.get("id") or data.get("order_id")

	if not entity_id:
		frappe.log_error(
			title="Medusa Webhook Missing Entity ID",
			message=json.dumps(
				{
					"event_name": event_name,
					"headers": _safe_headers(headers),
					"payload": redact(body),
				},
				indent=2,
				default=str,
			),
		)
		frappe.local.response["http_status_code"] = 400
		return {"status": "missing_entity_id"}

	webhook_event_id = event_id or data.get("webhook_event_id") or f"{event_name}:{entity_id}"

	if _is_duplicate_event(webhook_event_id):
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "duplicate_ignored",
			"event_id": webhook_event_id,
		}

	log = create_medusa_log(
		status="Queued",
		method="medusa_connector.webhook.dispatch.dispatch_event",
		message=webhook_message_key(webhook_event_id),
		request_data={
			"event_id": webhook_event_id,
			"event_name": event_name,
			"headers": _safe_headers(headers),
			"payload": redact(data),
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


def authenticate(settings, raw_body: bytes, headers: dict | None = None) -> bool:
	headers = headers or frappe.request.headers

	signature = headers.get("X-Medusa-Signature")
	timestamp = headers.get("X-Medusa-Timestamp")
	secret = settings.get_password("webhook_secret", raise_exception=False)

	if not secret or not signature or not timestamp:
		return False

	try:
		ts = int(timestamp)
	except ValueError:
		return False

	current_timestamp = now_datetime().timestamp()

	if abs(current_timestamp - ts) > MAX_TIMESTAMP_SKEW:
		return False

	signature = signature.removeprefix("sha256=")
	signed_content = f"{timestamp}.{raw_body.decode('utf-8')}"

	expected = hmac.new(
		secret.encode("utf-8"),
		signed_content.encode("utf-8"),
		hashlib.sha256,
	).hexdigest()

	return hmac.compare_digest(signature, expected)


def _is_duplicate_event(event_id: str) -> bool:
	"""Return True if the webhook event has already been processed recently."""
	cache = frappe.cache()
	key = f"medusa_webhook_seen:{event_id}"

	if cache.get_value(key):
		return True

	cache.set_value(key, 1, expires_in_sec=DEDUPE_WINDOW_SECONDS)
	return False


def _redact_raw(raw: bytes | str) -> str:
	"""Redact a raw webhook payload when JSON parsing has not yet occurred."""
	try:
		parsed = json.loads(raw or b"{}")
	except (json.JSONDecodeError, TypeError):
		return _decode_raw(raw)

	if not isinstance(parsed, dict):
		return _decode_raw(raw)

	return json.dumps(redact(parsed), default=str)


def _decode_raw(raw: bytes | str) -> str:
	"""Return the raw payload as a UTF-8 string for logging."""
	return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def _safe_headers(headers: dict) -> dict:
	"""Redact sensitive headers before logging."""
	return redact({str(key): str(value)[:500] for key, value in (headers or {}).items()})
