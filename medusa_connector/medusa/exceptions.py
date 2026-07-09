# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt


class MedusaConnectorError(Exception):
	"""Base error for the Medusa connector."""


class MedusaConnectionError(MedusaConnectorError):
	"""Raised when Medusa cannot be reached or returns an unexpected error."""


class MedusaAuthError(MedusaConnectorError):
	"""Raised when Medusa rejects the credentials (HTTP 401/403)."""
