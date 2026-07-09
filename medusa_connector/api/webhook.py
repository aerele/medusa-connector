# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Centralised inbound webhook receiver.

Responsibilities are deliberately narrow and fast: authenticate the request,
dedupe it, persist a Medusa Webhook Log row, and enqueue background processing.
No business logic runs here — that lives in the dispatcher and handlers.
"""

import base64
import hashlib
import hmac
import json

import frappe

EVENT_ID_HEADER = "X-Medusa-Event-Id"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict:
	"""Receive, authenticate, log and enqueue a Medusa webhook.

	URL: ``/api/method/medusa_connector.api.webhook.receive`` (optionally with a
	``?token=`` query param used for authenticity when the sender cannot sign).
	"""
	settings = frappe.get_cached_doc("Medusa Settings")

	if not settings.enabled or not settings.enable_webhook_processing:
		frappe.local.response["http_status_code"] = 503
		return {"status": "disabled"}

	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)

	authentic = authenticate(settings, raw, headers)
	if settings.verify_signatures and not authentic:
		_log(
			settings,
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

	event_id = body.get("id") or headers.get(EVENT_ID_HEADER)
	event_name = body.get("event") or body.get("name") or body.get("event_name")

	# Idempotency: Medusa delivers at-least-once, so a repeat id is a no-op.
	if event_id and frappe.db.exists("Medusa Webhook Log", {"event_id": event_id}):
		return {"status": "duplicate", "event_id": event_id}

	log = _log(
		settings,
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
	"""Return True if the request proves it came from the configured Medusa.

	Two mechanisms, in order of strength:

	1. **HMAC signature** header — used when the sender can sign (a custom
	   subscriber). The digest is ``hmac_sha256(secret, raw_body)``.
	2. **URL token** — used by the webhooks plugin, which cannot sign. The secret
	   is embedded as ``?token=`` in the registered endpoint and compared here.

	Both use constant-time comparison. With no secret configured, authentication
	fails closed (so ``verify_signatures`` must be off for unauthenticated setups).
	"""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return False

	sent = (headers.get(settings.webhook_signature_header) or "").strip()
	if sent:
		return _verify_hmac(secret, raw, sent, settings.webhook_signature_encoding)

	token = frappe.request.args.get("token") if frappe.request else None
	if token:
		return hmac.compare_digest(token, secret)

	return False


def _verify_hmac(secret: str, raw: bytes, sent: str, encoding: str) -> bool:
	digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
	expected = digest.hex() if encoding == "hex" else base64.b64encode(digest).decode()
	if sent.startswith("sha256="):
		sent = sent[len("sha256=") :]
	return hmac.compare_digest(sent, expected)


def _log(
	settings,
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
