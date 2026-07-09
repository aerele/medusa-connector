# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Single source of truth for the ERP webhook endpoint URL.

Both the receiver (what it answers on) and the sync service (what it registers
in Medusa) derive their URLs here, so a change to the site URL or the route is
picked up everywhere and the child table stays consistent automatically.
"""

from urllib.parse import urlsplit, urlunsplit

from frappe.utils import get_url

# Whitelisted guest endpoint that receives Medusa webhooks.
RECEIVER_METHOD = "/api/method/medusa_connector.api.webhook.receive"


def receiver_base_url() -> str:
	"""Absolute, query-less ERP endpoint shown to the operator and stored per row."""
	return get_url(RECEIVER_METHOD)


def signed_target_url(secret: str | None) -> str:
	"""URL actually registered in Medusa.

	The @lambdacurry/medusa-webhooks model stores no secret/header, so authenticity
	travels as a ``token`` query param that the receiver validates. When no secret
	is set the bare endpoint is used (only sensible with signature verification off).
	"""
	base = receiver_base_url()
	if not secret:
		return base
	return f"{base}?token={secret}"


def strip_query(url: str) -> str:
	"""Return ``url`` without its query string, for comparing endpoints."""
	if not url:
		return ""
	parts = urlsplit(url)
	return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
