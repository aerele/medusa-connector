# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import json

import frappe

EVENT_ID_HEADER = "X-Medusa-Event-Id"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict:
	"""Guest endpoint that receives, verifies, logs and enqueues a Medusa webhook.

	URL: ``/api/method/medusa_connector.api.webhook.receive``

	The handler stays fast: verify the HMAC signature over the raw body, dedupe
	on the Medusa event id, persist a Medusa Webhook Log row, enqueue dispatch,
	and return. All business logic runs in the background job.
	"""
	settings = frappe.get_cached_doc("Medusa Settings")

	if not settings.enabled or not settings.enable_webhook_processing:
		frappe.local.response["http_status_code"] = 503
		return {"status": "disabled"}

	raw = frappe.request.data or b""
	headers = dict(frappe.request.headers)

	signature_valid = verify_signature(settings, raw, headers)
	if settings.verify_signatures and not signature_valid:
		_log(
			settings,
			raw,
			headers,
			status="Rejected",
			signature_valid=False,
			error="Invalid or missing signature",
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
		signature_valid=signature_valid,
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


def verify_signature(settings, raw: bytes, headers: dict) -> bool:
	"""Verify the HMAC-SHA256 signature over the exact raw request body.

	Mirrors Frappe's own webhook signing (base64/hex of ``hmac_sha256(secret, body)``)
	and compares in constant time.
	"""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		return False

	sent = headers.get(settings.webhook_signature_header)
	if not sent:
		return False

	digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
	if settings.webhook_signature_encoding == "hex":
		expected = digest.hex()
	else:
		expected = base64.b64encode(digest).decode()

	# Tolerate a common ``sha256=`` prefix used by some subscribers.
	sent = sent.strip()
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
