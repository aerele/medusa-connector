# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Centralised inbound webhook receiver.

Responsibilities are deliberately narrow and fast: authenticate the request,
dedupe it, persist a Medusa Webhook Log row, and enqueue background processing.
No business logic runs here — that lives in the dispatcher and handlers.
"""

import hashlib
import hmac
import json

import frappe

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
		_log(
			raw,
			headers,
			status="Rejected",
			signature_valid=False,
			error="Invalid or missing signature/token",
		)
		frappe.local.response["http_status_code"] = 401
		return {"status": "rejected"}

	try:
		body = json.loads(raw or b"{}")
	except (ValueError, TypeError):
		body = {}

	# The plugin's ``id`` is the affected resource id, not a unique webhook
	# delivery id. Do not use it for deduplication: doing so would suppress every
	# later update to the same product. Prefer an explicit delivery id/header and
	# generate one when the plugin provides neither.
	event_id = headers.get(EVENT_ID_HEADER) or body.get("event_id") or frappe.generate_hash(length=16)
	# The Medusa webhooks plugin delivers ``{"id": "..."}`` only. The subscribed
	# event is carried in the callback URL by WebhookSyncService.
	event_name = (
		body.get("event") or body.get("name") or body.get("event_name") or frappe.request.args.get("event")
	)

	# Idempotency: Medusa delivers at-least-once, so a repeat id is a no-op.
	if event_id and frappe.db.exists("Medusa Webhook Log", {"event_id": event_id}):
		return {"status": "duplicate", "event_id": event_id}

	log = _log(
		raw,
		headers,
		status="Queued",
		signature_valid=authentic,
		event_id=event_id,
		event_name=event_name,
	)

	frappe.enqueue(
		"medusa_connector.webhook.dispatch.dispatch_event",
		queue="short",
		enqueue_after_commit=True,
		log_name=log.name,
	)

	return {"status": "accepted", "log": log.name}


def authenticate(settings, raw: bytes, headers: dict) -> bool:
	"""Return True if the request proves it came from the configured Medusa."""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return False

	# Prefer HMAC when a signature header is present.
	sent = _get_header(headers, _signature_header_name(settings))
	if sent:
		return _verify_hmac(secret, raw, sent)

	# Plugin compatibility: target_url can only carry a query token, not a signature.
	token = frappe.request.args.get("token") if frappe.request else None
	if token:
		return hmac.compare_digest(token, secret)

	return False


def _signature_header_name(settings) -> str:
	"""Header name for HMAC verification; configurable with a sensible default."""
	name = (settings.get("webhook_signature_header") or "").strip()
	return name or DEFAULT_SIGNATURE_HEADER


def _get_header(headers: dict, name: str) -> str:
	"""Return a request header value (case-insensitive), or empty string."""
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
	"""Verify HMAC-SHA256 of the raw body; expected encoding is hexadecimal only."""
	expected_hex = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
	if sent.startswith("sha256="):
		sent = sent[len("sha256=") :]
	return hmac.compare_digest(sent, expected_hex)


def _log(
	raw: bytes,
	headers: dict,
	status: str,
	signature_valid: bool,
	event_id: str | None = None,
	event_name: str | None = None,
	error: str | None = None,
):
	"""Create a Medusa Webhook Log row (guest context → ignore permissions)."""
	body_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
	doc = frappe.get_doc(
		{
			"doctype": "Medusa Webhook Log",
			"event_id": event_id,
			"event_name": event_name,
			"status": status,
			"signature_valid": 1 if signature_valid else 0,
			"source_ip": frappe.local.request_ip,
			"request_headers": frappe.as_json(headers),
			"payload": body_text,
			"error": error,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc
