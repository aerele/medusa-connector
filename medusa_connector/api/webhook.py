# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Centralised inbound webhook receiver.

Responsibilities are deliberately narrow and fast: authenticate the request,
dedupe it, persist an Ecommerce Integration Log row, and enqueue background
processing. No business logic runs here — that lives in the dispatcher and handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe

from medusa_connector.constants import MODULE_NAME
from medusa_connector.utils.logging import (
	create_medusa_log,
	find_webhook_log_by_event_id,
	webhook_message_key,
)

EVENT_ID_HEADER = "X-Medusa-Event-Id"
DEFAULT_SIGNATURE_HEADER = "X-Medusa-Signature"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict:
	"""Receive, authenticate, log and enqueue a Medusa webhook.

	URL: ``/api/method/medusa_connector.api.webhook.receive`` (optionally with a
	``?token=`` query param — required for the Medusa webhooks plugin, which cannot
	HMAC-sign deliveries).
	"""
	settings = frappe.get_cached_doc("Medusa Settings")

	if not settings.enabled:
		frappe.local.response["http_status_code"] = 503
		return {"status": "disabled"}

	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)

	authentic = authenticate(settings, raw, headers)
	if settings.verify_signatures and not authentic:
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
		body = {}

	# Delivery id for at-least-once dedupe (not the resource id).
	event_id = headers.get(EVENT_ID_HEADER) or body.get("event_id") or frappe.generate_hash(length=16)
	event_name = (
		body.get("event") or body.get("name") or body.get("event_name") or frappe.request.args.get("event")
	)

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


def authenticate(settings, raw: bytes, headers: dict) -> bool:
	"""Return True if the request proves it came from the configured Medusa."""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return False

	sent = _get_header(headers, _signature_header_name(settings))
	if sent:
		return _verify_hmac(secret, raw, sent)

	token = frappe.request.args.get("token") if frappe.request else None
	if token:
		return hmac.compare_digest(token, secret)

	return False


def _signature_header_name(settings) -> str:
	name = (settings.get("webhook_signature_header") or "").strip()
	return name or DEFAULT_SIGNATURE_HEADER


def _get_header(headers: dict, name: str) -> str:
	if not name:
		return ""
	value = headers.get(name)
	if value:
		return str(value).strip()
	lower = name.lower()
	for key, value in headers.items():
		if key.lower() == lower and value:
			return str(value).strip()
	return ""


def _verify_hmac(secret: str, raw: bytes, sent: str) -> bool:
	expected_hex = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
	if sent.startswith("sha256="):
		sent = sent[len("sha256=") :]
	return hmac.compare_digest(sent, expected_hex)


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
