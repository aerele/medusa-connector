# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Single source of truth for the ERP webhook endpoint URL.

Both the receiver (what it answers on) and the sync service (what it registers
in Medusa) derive their URLs here, so a change to the site URL or the route is
picked up everywhere and the child table stays consistent automatically.
"""

from urllib.parse import urlencode, urlsplit, urlunsplit

from frappe.utils import get_url

# Whitelisted guest endpoint that receives Medusa webhooks.
RECEIVER_METHOD = "/api/method/medusa_connector.api.webhook.receive"


def receiver_base_url() -> str:
	"""Absolute, query-less ERP endpoint shown to the operator and stored per row."""
	return get_url(RECEIVER_METHOD)


def signed_target_url(secret: str | None, event: str | None = None) -> str:
	"""URL actually registered in Medusa.

	The @lambdacurry/medusa-webhooks model stores no secret/header, so authenticity
	travels as a ``token`` query param that the receiver validates. The plugin
	delivers only a resource id, so the subscribed event is also included in the
	callback URL for the receiver to route the delivery correctly.
	"""
	base = receiver_base_url()
	params = {}
	if secret:
		params["token"] = secret
	if event:
		params["event"] = event
	return f"{base}?{urlencode(params)}" if params else base


def strip_query(url: str) -> str:
	"""Return ``url`` without its query string, for comparing endpoints."""
	if not url:
		return ""
	parts = urlsplit(url)
	return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
