# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Logging utilities for Medusa sync and webhook operations.

Provides centralized logging for sync outcomes and the ``@logged_sync``
decorator for consistent Success, Invalid, and Error status handling.
"""

from __future__ import annotations

import functools

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)
from frappe.utils import cstr, now_datetime

from medusa_connector.constants import _RESULT_STATUS_MAP, MODULE_NAME, SENSITIVE_KEYS


def _is_sensitive_key(key: str) -> bool:
	key = key.lower()
	return any(field in key for field in SENSITIVE_KEYS)


def redact(value):
	"""Recursively mask known-sensitive keys in a dict/list structure.
	Safe to call on anything — non dict/list values pass through unchanged."""
	if isinstance(value, dict):
		return {k: ("***REDACTED***" if _is_sensitive_key(str(k)) else redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		return [redact(item) for item in value]
	return value


def create_medusa_log(**kwargs):
	if kwargs.get("status"):
		kwargs["status"] = _normalize_status(kwargs["status"])
	if isinstance(kwargs.get("request_data"), (dict, list)):
		kwargs["request_data"] = redact(kwargs["request_data"])
	if isinstance(kwargs.get("response_data"), (dict, list)):
		kwargs["response_data"] = redact(kwargs["response_data"])
	return create_log(module_def=MODULE_NAME, **kwargs)


def _normalize_status(status: str) -> str:
	"""Map connector statuses onto Ecommerce Integration Log values."""
	mapping = {
		"Failed": "Error",
		"Processed": "Success",
		"Rejected": "Error",
		"Running": "Queued",
		"Partial Success": "Error",
	}
	return mapping.get(status, status)


def create_sync_log(
	*,
	sync_type: str = "Other",
	sync_mode: str | None = None,
	status: str = "Queued",
	method: str | None = None,
	message: str | None = None,
	request_data=None,
) -> str:
	"""Compatibility helper used by product/customer/inventory jobs.
	Returns the Ecommerce Integration Log name (used as ``request_id``).
	"""
	payload = {}
	if isinstance(request_data, dict):
		payload.update(request_data)
	elif request_data is not None:
		payload["data"] = request_data
	payload.setdefault("sync_type", sync_type)
	if sync_mode:
		payload["sync_mode"] = sync_mode

	log = create_medusa_log(
		status=status,
		method=method,
		message=message or sync_type,
		request_data=payload,
		make_new=True,
	)
	frappe.flags.request_id = log.name
	return log.name


def update_sync_log(
	name: str,
	*,
	status: str | None = None,
	message: str | None = None,
	created: int | None = None,
	updated: int | None = None,
	failed: int | None = None,
	skipped: int | None = None,
	failed_ids=None,
	summary=None,
	traceback: str | None = None,
	complete: bool = False,
) -> None:
	if not name or not frappe.db.exists("Ecommerce Integration Log", name):
		return

	frappe.flags.request_id = name
	response = {
		"summary": summary,
		"created": created,
		"updated": updated,
		"failed": failed,
		"skipped": skipped,
		"failed_ids": failed_ids,
	}
	response = {k: v for k, v in response.items() if v is not None}
	if complete:
		response["completed_at"] = str(now_datetime())

	if traceback:
		frappe.db.set_value(
			"Ecommerce Integration Log",
			name,
			"traceback",
			traceback,
			update_modified=False,
		)

	create_medusa_log(
		make_new=False,
		status=status or "Success",
		message=message,
		response_data=response or None,
	)


def webhook_message_key(event_id: str) -> str:
	"""Stable message key used for webhook delivery dedupe."""
	return f"webhook:{cstr(event_id)}"


def logged_sync(dotted_method: str):
	def decorator(func):
		@functools.wraps(func)
		def wrapper(self, *args, request_id: str | None = None, **kwargs):
			frappe.set_user("Administrator")  # nosemgrep
			if request_id:
				frappe.flags.request_id = request_id

			request_data = _sync_request_data(args, kwargs)

			try:
				value = func(self, *args, request_id=request_id, **kwargs)
			except Exception as exc:
				create_medusa_log(
					status="Error",
					exception=exc,
					rollback=True,
					request_data=request_data,
					method=dotted_method,
				)
				if request_id:
					frappe.flags.request_id = request_id
				return None

			status, message, response_data = _describe_result(value)
			create_medusa_log(
				status=status,
				message=message,
				request_data=request_data,
				response_data=response_data,
				method=dotted_method,
			)
			return value

		return wrapper

	return decorator


def _describe_result(value):
	if isinstance(value, dict) and "status" in value:
		status = _RESULT_STATUS_MAP.get(cstr(value["status"]).lower(), "Error")
		return status, value.get("message"), value
	return "Success", None, value


def _sync_request_data(args, kwargs):
	if args and isinstance(args[0], dict):
		return args[0]
	data = {k: v for k, v in kwargs.items() if v is not None}
	return data or None
