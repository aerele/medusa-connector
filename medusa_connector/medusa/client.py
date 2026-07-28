# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Authenticated REST client for the Medusa Admin API."""

from __future__ import annotations

import frappe
import requests

REST_HEALTH_PATH = "/admin/regions"
WEBHOOKS_PATH = "/admin/webhooks"


class MedusaClient:
	"""Authenticated client for communicating with the Medusa Admin API."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc("Medusa Settings")

		if not self.settings.enabled:
			raise ValueError("Medusa Connector is disabled")

		if not self.settings.medusa_base_url:
			raise ValueError("Medusa Base URL is not configured.")

		self.base_url = self.settings.medusa_base_url
		self._api_key = self._resolve_api_key()

	def _resolve_api_key(self) -> str | None:
		"""Return the configured Medusa Admin API key."""
		return self.settings.get_password("admin_api_key", raise_exception=False)

	def _headers(self) -> dict:
		return {
			"Content-Type": "application/json",
			"Accept": "application/json",
		}

	def _auth(self):
		"""Return HTTP Basic authentication credentials for the Medusa API."""
		if self._api_key:
			return (self._api_key, "")
		return None

	def _request(self, method: str, url: str, **kwargs) -> dict:
		"""Send an authenticated request and return the parsed response."""
		response = requests.request(
			method,
			url,
			auth=self._auth(),
			headers=self._headers(),
			timeout=30,
			**kwargs,
		)

		if not response.ok:
			try:
				error_data = response.json()
			except ValueError:
				error_data = response.text

			raise requests.HTTPError(
				f"{response.status_code} {response.reason} for {url}: {error_data}",
				response=response,
			)

		return self._parse(response)

	@staticmethod
	def _parse(response) -> dict:
		"""Parse a JSON response and return an empty dict for empty responses."""
		if response.status_code == 204 or not response.content:
			return {}

		content_type = response.headers.get("content-type", "")
		if "json" not in content_type:
			return {}

		try:
			body = response.json()
		except ValueError:
			return {}

		return body if isinstance(body, dict) else {"data": body}

	def execute_rest(
		self,
		method: str,
		path: str,
		params: dict | None = None,
		json: dict | None = None,
	) -> dict:
		"""Call a Medusa REST endpoint."""
		url = f"{self.base_url}/{path.lstrip('/')}"
		return self._request(method, url, params=params, json=json)

	def health_check(self) -> None:
		"""Verify connectivity to the Medusa Admin API."""
		self.execute_rest("GET", REST_HEALTH_PATH, params={"limit": 1})

	def webhook_plugin_installed(self) -> bool:
		"""Return whether the Medusa webhook plugin API is available."""
		try:
			self.execute_rest("GET", WEBHOOKS_PATH, params={"limit": 1})
		except requests.HTTPError as exc:
			if exc.response is not None and exc.response.status_code == 404:
				return False
			raise

		return True

	def list_webhooks(self) -> list[dict]:
		"""Return all webhook subscriptions registered in Medusa."""
		subscriptions: list[dict] = []
		offset, limit = 0, 100

		while True:
			response = self.execute_rest(
				"GET",
				WEBHOOKS_PATH,
				params={"limit": limit, "offset": offset},
			)
			page = response.get("subscriptions") or response.get("webhooks") or []
			subscriptions.extend(page)

			count = response.get("count")
			offset += limit

			if not page or count is None or offset >= count:
				break

		return subscriptions

	def create_webhook(
		self,
		event_type: str,
		target_url: str,
		active: bool = True,
	) -> dict:
		"""Create a webhook subscription in Medusa."""
		response = self.execute_rest(
			"POST",
			WEBHOOKS_PATH,
			json={
				"event_type": event_type,
				"target_url": target_url,
				"active": active,
			},
		)
		return response.get("webhook") or response.get("subscription") or response

	def update_webhook(
		self,
		webhook_id: str,
		event_type: str,
		target_url: str,
		active: bool = True,
	) -> dict:
		"""Update an existing webhook subscription in Medusa."""
		response = self.execute_rest(
			"POST",
			f"{WEBHOOKS_PATH}/{webhook_id}",
			json={
				"event_type": event_type,
				"target_url": target_url,
				"active": active,
			},
		)
		return response.get("webhook") or response.get("subscription") or response

	def delete_webhook(self, webhook_id: str) -> None:
		"""Delete a webhook subscription from Medusa."""
		self.execute_rest("DELETE", f"{WEBHOOKS_PATH}/{webhook_id}")


def get_client() -> MedusaClient:
	"""Return the request-scoped Medusa API client."""
	if not getattr(frappe.local, "_medusa_client", None):
		frappe.local._medusa_client = MedusaClient()

	return frappe.local._medusa_client


def execute_rest(
	method: str,
	path: str,
	params: dict | None = None,
	json: dict | None = None,
) -> dict:
	"""Execute a REST request using the request-scoped Medusa client."""
	return get_client().execute_rest(method, path, params=params, json=json)
