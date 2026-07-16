# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import hmac
import json

import frappe

from medusa_connector.constants import MODULE_NAME
from medusa_connector.utils.logging import (
	create_medusa_log,
	find_webhook_log_by_event_id,
	webhook_message_key,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def receive() -> dict:
	"""Receive, authenticate, log and enqueue a Medusa webhook.

	URL: ``/api/method/medusa_connector.api.webhook.receive`` (optionally with a
	``?token=`` query param — required for the Medusa webhooks plugin, which cannot
	HMAC-sign deliveries).
	"""
	settings = frappe.get_cached_doc("Medusa Settings")

	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)
	authentic = authenticate(settings)
	if not authentic:
		create_medusa_log(
			status="Error",
			method="medusa_connector.api.webhook.receive",
			message="webhook:rejected",
			request_data={
				"headers": _safe_headers(headers),
				"payload": _decode_raw(raw),
				"signature_valid": False,
			},
			exception="Invalid or missing signature/token",
			make_new=True,
		)
		frappe.local.response["http_status_code"] = 401
		return {"status": "rejected"}

	try:
		body = json.loads(raw or b"{}")
	except (ValueError, TypeError):
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid_payload"}

	# Delivery id for at-least-once dedupe (not the resource id).
	event_id = body.get("event_id") or frappe.generate_hash(length=16)
	event_name = frappe.request.args.get("event") or body.get("event")

	if event_id and find_webhook_log_by_event_id(event_id):
		return {"status": "duplicate", "event_id": event_id}

	log = create_medusa_log(
		status="Queued",
		method="medusa_connector.webhook.dispatch.dispatch_event",
		message=webhook_message_key(event_id),
		request_data={
			"event_id": event_id,
			"event_name": event_name,
			"headers": _safe_headers(headers),
			"payload": body if body else _decode_raw(raw),
			"signature_valid": authentic,
			"integration": MODULE_NAME,
		},
		make_new=True,
	)

	frappe.enqueue(
		"medusa_connector.webhook.dispatch.dispatch_event",
		queue="short",
		enqueue_after_commit=True,
		log_name=log.name,
		request_id=log.name,
	)

	return {"status": "accepted", "log": log.name}


def authenticate(settings) -> bool:
	"""Return True if the request proves it came from the configured Medusa."""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return False

	token = frappe.request.args.get("token")
	return bool(token) and hmac.compare_digest(token, secret)


def _decode_raw(raw: bytes | str) -> str:
	if isinstance(raw, bytes):
		return raw.decode("utf-8", errors="replace")
	return str(raw)


def _safe_headers(headers: dict) -> dict:
	"""Drop huge/binary values so the log stays readable."""
	out = {}
	for key, value in (headers or {}).items():
		try:
			out[str(key)] = str(value)[:500]
		except Exception:
			continue
	return out
