# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa-facing wrappers around Ecommerce Core integration logging.

All sync/webhook outcomes go to **Ecommerce Integration Log** via ``create_log(module_def=...)``.
"""

from __future__ import annotations

import json

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)
from frappe.utils import cstr, now_datetime

from medusa_connector.constants import MODULE_NAME


def create_medusa_log(**kwargs):
	"""Create or update an Ecommerce Integration Log for this integration."""
	if kwargs.get("status"):
		kwargs["status"] = _normalize_status(kwargs["status"])
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
	"""Update an existing Ecommerce Integration Log row."""
	if not name or not frappe.db.exists("Ecommerce Integration Log", name):
		return

	frappe.flags.request_id = name
	response: dict = {}
	if summary is not None:
		response["summary"] = summary
	if created is not None:
		response["created"] = created
	if updated is not None:
		response["updated"] = updated
	if failed is not None:
		response["failed"] = failed
	if skipped is not None:
		response["skipped"] = skipped
	if failed_ids is not None:
		response["failed_ids"] = failed_ids
	if complete:
		response["completed_at"] = str(now_datetime())

	kwargs = {
		"make_new": False,
		"status": status or "Success",
		"message": message,
		"response_data": response or None,
	}
	if traceback:
		# create_log only auto-fills traceback from frappe.get_traceback on save;
		# set explicitly when provided.
		log = frappe.get_doc("Ecommerce Integration Log", name)
		log.db_set("traceback", traceback, update_modified=False)

	create_medusa_log(**{k: v for k, v in kwargs.items() if v is not None})


def webhook_message_key(event_id: str) -> str:
	"""Stable message key used for webhook delivery dedupe."""
	return f"webhook:{cstr(event_id)}"


def find_webhook_log_by_event_id(event_id: str) -> str | None:
	if not event_id:
		return None
	return frappe.db.get_value(
		"Ecommerce Integration Log",
		{"integration": MODULE_NAME, "message": webhook_message_key(event_id)},
		"name",
	)
