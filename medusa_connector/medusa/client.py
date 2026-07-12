# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import get_request_session, now_datetime

from medusa_connector.medusa.exceptions import (
	MedusaAuthError,
	MedusaConnectionError,
	WebhookPluginNotInstalled,
)

# A trivial GraphQL query used as a GraphQL-mode health check. `__typename` on the
# root query type is always resolvable when the endpoint is reachable/authenticated.
HEALTH_QUERY = "query { __typename }"

# A lightweight authenticated admin endpoint used as a REST-mode health check.
REST_HEALTH_PATH = "/admin/regions"

# Admin route exposed by the @lambdacurry/medusa-webhooks plugin.
WEBHOOKS_PATH = "/admin/webhooks"

# Per-request timeout (seconds). Handled in code rather than a settings field.
DEFAULT_TIMEOUT = 30


def get_settings():
	"""Return the cached Medusa Settings single doc."""
	return frappe.get_cached_doc("Medusa Settings")


class MedusaClient:
	"""Thin authenticated client that talks to Medusa in REST or GraphQL mode.

	With a static admin API key there is no login/token lifecycle: the "session"
	is the pooled requests.Session inside ``make_request`` plus the auth header
	assembled here. The client is cached per request on ``frappe.local``.
	"""

	def __init__(self, settings=None) -> None:
		self.settings = settings or get_settings()
		if not self.settings.enabled:
			raise MedusaConnectionError("Medusa Connector is disabled")

		self.mode = self.settings.connection_mode or "REST"
		if self.mode == "GraphQL":
			if not self.settings.graphql_url:
				raise MedusaConnectionError(
					"GraphQL URL is not configured. Set it, or switch Connection Mode to REST."
				)
			self.graphql_url = self.settings.graphql_url
		else:
			if not self.settings.medusa_base_url:
				raise MedusaConnectionError(
					"Please enter the Medusa Base URL before enabling the Medusa Connector."
				)
			# Normalise: drop a trailing slash so path joins are predictable.
			self.base_url = self.settings.medusa_base_url.rstrip("/")

		self.timeout = DEFAULT_TIMEOUT
		self._api_key = self._resolve_api_key()

	def _resolve_api_key(self) -> str | None:
		"""Return the admin API key, preferring a freshly typed value on an unsaved doc.

		During ``validate`` (e.g. the enable-time health check) the key the user just
		entered lives in the in-memory field; ``get_password`` would still read the
		older stored value from the DB. A saved key comes back via ``get_password``.
		Frappe masks the field with an ``X``/``*`` placeholder when unchanged, so a
		value made only of those characters is not a real key.
		"""
		value = self.settings.get("admin_api_key")
		if value and set(value) - {"X", "*"}:
			return value
		return self.settings.get_password("admin_api_key", raise_exception=False)

	def _headers(self) -> dict:
		return {"Content-Type": "application/json"}

	def _auth(self):
		if self._api_key:
			# Medusa v2 secret keys authenticate via HTTP Basic auth: the key is the
			# username and the password is empty (equivalent to ``curl -u "sk_...:"``).
			return (self._api_key, "")
		return None

	def _request(self, method: str, url: str, **kwargs) -> dict:
		"""Issue an authenticated request with a hard timeout, mapping failures.

		Uses the pooled request session directly (rather than ``make_request``) so a
		connect/read ``timeout`` is always enforced — a background handler must never
		hang forever on an unresponsive Medusa. Auth failures map to MedusaAuthError;
		everything else to MedusaConnectionError, both carrying the HTTP status.
		"""
		try:
			session = get_request_session()
			response = session.request(
				method,
				url,
				auth=self._auth(),
				headers=self._headers(),
				timeout=self.timeout,
				**kwargs,
			)
			response.raise_for_status()
		except Exception as exc:
			status = getattr(getattr(exc, "response", None), "status_code", None)
			if status in (401, 403):
				raise MedusaAuthError(
					f"Medusa rejected the credentials (HTTP {status})", status_code=status
				) from exc
			raise MedusaConnectionError(str(exc), status_code=status) from exc
		return self._parse(response)

	@staticmethod
	def _parse(response) -> dict:
		"""Return the JSON body as a dict, or ``{}`` for empty/non-JSON responses."""
		if response.status_code == 204 or not response.content:
			return {}
		content_type = response.headers.get("content-type", "")
		if "json" in content_type:
			try:
				body = response.json()
			except ValueError:
				return {}
			return body if isinstance(body, dict) else {"data": body}
		return {}

	def execute_graphql(self, query: str, variables: dict | None = None) -> dict:
		"""Run a GraphQL query. Only valid when Connection Mode is GraphQL."""
		if self.mode != "GraphQL":
			raise MedusaConnectionError("execute_graphql requires Connection Mode = GraphQL")
		payload = {"query": query, "variables": variables or {}}
		response = self._request("POST", self.graphql_url, json=payload)
		if isinstance(response, dict) and response.get("errors"):
			raise MedusaConnectionError(frappe.as_json(response["errors"]))
		return response

	def execute_rest(
		self, method: str, path: str, params: dict | None = None, json: dict | None = None
	) -> dict:
		"""Call a Medusa REST endpoint. ``path`` is joined onto the base URL (e.g. ``/admin/orders``)."""
		if self.mode != "REST":
			raise MedusaConnectionError("execute_rest requires Connection Mode = REST")
		url = f"{self.base_url}/{path.lstrip('/')}"
		return self._request(method, url, params=params, json=json)

	def health_check(self) -> None:
		"""Issue the mode-appropriate probe; raises on failure."""
		if self.mode == "GraphQL":
			self.execute_graphql(HEALTH_QUERY)
		else:
			self.execute_rest("GET", REST_HEALTH_PATH, params={"limit": 1})

	# ------------------------------------------------------------------
	# Webhooks plugin admin API (@lambdacurry/medusa-webhooks)
	#
	# The plugin exposes CRUD under /admin/webhooks. Its subscription model is
	# minimal — {id, event_type, target_url, active} — with no secret/header
	# field, which is why authenticity is carried by a token embedded in the
	# registered target_url (see the receiver). All methods require REST mode.
	# ------------------------------------------------------------------
	def _require_rest(self) -> None:
		if self.mode != "REST":
			raise MedusaConnectionError(
				"Webhook management requires Connection Mode = REST (the plugin exposes a REST admin API)."
			)

	def webhook_plugin_installed(self) -> bool:
		"""Return True if the webhooks plugin routes exist, False on a 404."""
		self._require_rest()
		try:
			self.execute_rest("GET", WEBHOOKS_PATH, params={"limit": 1})
			return True
		except MedusaConnectionError as exc:
			if exc.status_code == 404:
				return False
			raise

	@staticmethod
	def _unwrap_subscription(resp: dict | None) -> dict:
		"""Normalise create/update responses to a single subscription dict.

		The plugin returns ``{"subscription": {...}}``. MedusaService helpers
		sometimes serialise a one-item list — accept both shapes.
		"""
		if not resp or not isinstance(resp, dict):
			return {}
		data = resp.get("webhook") or resp.get("subscription") or resp
		if isinstance(data, list):
			return data[0] if data and isinstance(data[0], dict) else {}
		return data if isinstance(data, dict) else {}

	def list_webhooks(self) -> list[dict]:
		"""Return every webhook subscription registered in Medusa (all pages)."""
		self._require_rest()
		subscriptions: list[dict] = []
		offset, limit = 0, 100
		while True:
			resp = self.execute_rest("GET", WEBHOOKS_PATH, params={"limit": limit, "offset": offset})
			if resp.get("statusCode") == 404 or resp is None:
				raise WebhookPluginNotInstalled("Medusa webhooks plugin is not installed")
			page = resp.get("subscriptions") or resp.get("webhooks") or []
			if not isinstance(page, list):
				page = []
			subscriptions.extend(page)
			count = resp.get("count")
			offset += limit
			if not page or count is None or offset >= count:
				break
		return subscriptions

	def create_webhook(self, event_type: str, target_url: str, active: bool = True) -> dict:
		"""Register a new webhook subscription and return the created record."""
		self._require_rest()
		resp = self.execute_rest(
			"POST",
			WEBHOOKS_PATH,
			json={"event_type": event_type, "target_url": target_url, "active": active},
		)
		return self._unwrap_subscription(resp)

	def update_webhook(self, webhook_id: str, event_type: str, target_url: str, active: bool = True) -> dict:
		"""Update an existing webhook subscription (event, URL, active flag)."""
		self._require_rest()
		resp = self.execute_rest(
			"PUT",
			f"{WEBHOOKS_PATH}/{webhook_id}",
			json={
				"id": webhook_id,
				"event_type": event_type,
				"target_url": target_url,
				"active": active,
			},
		)
		return self._unwrap_subscription(resp)

	def delete_webhook(self, webhook_id: str) -> None:
		"""Delete a webhook subscription by its Medusa id."""
		self._require_rest()
		self.execute_rest("DELETE", f"{WEBHOOKS_PATH}/{webhook_id}")


def get_client() -> MedusaClient:
	"""Return a per-request cached MedusaClient."""
	if not getattr(frappe.local, "_medusa_client", None):
		frappe.local._medusa_client = MedusaClient()
	return frappe.local._medusa_client


def execute_graphql(query: str, variables: dict | None = None) -> dict:
	"""Module-level convenience wrapper around ``MedusaClient.execute_graphql``."""
	return get_client().execute_graphql(query, variables)


def execute_rest(method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
	"""Module-level convenience wrapper around ``MedusaClient.execute_rest``."""
	return get_client().execute_rest(method, path, params=params, json=json)


def _update_status(status: str, message: str) -> None:
	"""Persist the connection health onto Medusa Settings."""
	settings = frappe.get_single("Medusa Settings")
	settings.db_set(
		{
			"connection_status": status,
			"last_connection_test": now_datetime(),
			"last_connection_message": message,
		},
		commit=True,
	)


def test_connection() -> dict:
	"""Run the mode-appropriate health probe, persist the result, and return a summary."""
	try:
		# Build a fresh client so a just-saved mode/key/url is picked up.
		client = MedusaClient()
		client.health_check()
	except MedusaAuthError as exc:
		_update_status("Auth Failed", str(exc))
		return {"status": "Auth Failed", "message": str(exc)}
	except MedusaConnectionError as exc:
		_update_status("Error", str(exc))
		return {"status": "Error", "message": str(exc)}

	message = f"Connection successful ({client.mode})."
	_update_status("Connected", message)
	return {"status": "Connected", "message": message}


def scheduled_health_check() -> None:
	"""Hourly scheduler hook: refresh connection status when the connector is enabled."""
	if not frappe.db.get_single_value("Medusa Settings", "enabled"):
		return
	test_connection()
