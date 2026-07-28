from urllib.parse import urlencode, urlsplit, urlunsplit

from frappe.utils import get_url

from medusa_connector.constants import RECEIVER_METHOD


def receiver_base_url() -> str:
	return get_url(RECEIVER_METHOD)


def signed_target_url(secret: str | None, event: str | None = None) -> str:
	base_url = receiver_base_url()
	params = {}

	if secret:
		params["token"] = secret

	if event:
		params["event"] = event

	return f"{base_url}?{urlencode(params)}" if params else base_url


def strip_query(url: str) -> str:
	if not url:
		return ""

	parts = urlsplit(url)
	return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
