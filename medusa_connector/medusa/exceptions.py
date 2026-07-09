# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt


class MedusaConnectorError(Exception):
	"""Base error for the Medusa connector.

	Carries the originating HTTP ``status_code`` (when known) so callers can
	branch on it — e.g. treat a 404 on the webhooks route as "plugin not installed".
	"""

	def __init__(self, message: str, status_code: int | None = None) -> None:
		super().__init__(message)
		self.status_code = status_code


class MedusaConnectionError(MedusaConnectorError):
	"""Raised when Medusa cannot be reached or returns an unexpected error."""


class MedusaAuthError(MedusaConnectorError):
	"""Raised when Medusa rejects the credentials (HTTP 401/403)."""


class WebhookPluginNotInstalled(MedusaConnectorError):
	"""Raised when the Medusa webhooks plugin routes are absent (HTTP 404)."""
