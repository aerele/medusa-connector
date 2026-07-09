# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.integrations.utils import make_request
from frappe.utils import now_datetime

from medusa_connector.medusa.exceptions import MedusaAuthError, MedusaConnectionError

# A trivial GraphQL query used as a GraphQL-mode health check. `__typename` on the
# root query type is always resolvable when the endpoint is reachable/authenticated.
HEALTH_QUERY = "query { __typename }"

# A lightweight authenticated admin endpoint used as a REST-mode health check.
REST_HEALTH_PATH = "/admin/regions"


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
					"Medusa Base URL is not configured. Set it, or switch Connection Mode to GraphQL."
				)
			# Normalise: drop a trailing slash so path joins are predictable.
			self.base_url = self.settings.medusa_base_url.rstrip("/")

		self.timeout = self.settings.request_timeout or 30
		self._api_key = self.settings.get_password("admin_api_key", raise_exception=False)

	def _headers(self) -> dict:
		headers = {"Content-Type": "application/json"}
		if self.settings.pass_through_auth and self._api_key:
			# Medusa admin convention: raw key in the Authorization header.
			headers["Authorization"] = self._api_key
		return headers

	def _request(self, method: str, url: str, **kwargs) -> dict:
		"""Shared request wrapper that maps auth failures to MedusaAuthError."""
		try:
			response = make_request(method, url, headers=self._headers(), **kwargs)
		except Exception as exc:
			status = getattr(getattr(exc, "response", None), "status_code", None)
			if status in (401, 403):
				raise MedusaAuthError(f"Medusa rejected the credentials (HTTP {status})") from exc
			raise MedusaConnectionError(str(exc)) from exc
		return response or {}

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
