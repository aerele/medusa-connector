# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, strip_html


class MedusaSyncLog(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		completed_at: DF.Datetime | None
		created_count: DF.Int
		failed_count: DF.Int
		failed_ids: DF.SmallText | None
		message: DF.SmallText | None
		method: DF.Data | None
		request_data: DF.Code | None
		response_data: DF.Code | None
		skipped_count: DF.Int
		started_at: DF.Datetime | None
		status: DF.Literal["Queued", "Running", "Success", "Partial Success", "Failed"]
		summary_json: DF.Code | None
		sync_mode: DF.Literal["", "Full", "Incremental"]
		sync_type: DF.Literal[
			"Product Import", "Product Export", "Product Single", "Webhook", "Health Check", "Other"
		]
		title: DF.Data | None
		traceback: DF.Code | None
		updated_count: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		if not self.title:
			self.title = self.message or self.sync_type or self.name
		self.title = strip_html(self.title or "")[:140]


def create_sync_log(
	*,
	sync_type: str = "Other",
	sync_mode: str | None = None,
	status: str = "Queued",
	method: str | None = None,
	message: str | None = None,
	request_data=None,
) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Medusa Sync Log",
			"sync_type": sync_type,
			"sync_mode": sync_mode,
			"status": status,
			"method": method,
			"message": message,
			"request_data": _as_json(request_data),
			"started_at": now_datetime() if status in ("Running", "Queued") else None,
			"title": message or sync_type,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def update_sync_log(
	name: str,
	*,
	status: str | None = None,
	message: str | None = None,
	created: int | None = None,
	updated: int | None = None,
	skipped: int | None = None,
	failed: int | None = None,
	failed_ids: list[str] | None = None,
	summary: dict | None = None,
	response_data=None,
	traceback: str | None = None,
	complete: bool = False,
) -> None:
	values: dict = {}
	if status is not None:
		values["status"] = status
	if message is not None:
		values["message"] = message
		values["title"] = strip_html(message)[:140]
	if created is not None:
		values["created_count"] = created
	if updated is not None:
		values["updated_count"] = updated
	if skipped is not None:
		values["skipped_count"] = skipped
	if failed is not None:
		values["failed_count"] = failed
	if failed_ids is not None:
		values["failed_ids"] = ",".join(failed_ids)
	if summary is not None:
		values["summary_json"] = _as_json(summary)
	if response_data is not None:
		values["response_data"] = _as_json(response_data)
	if traceback is not None:
		values["traceback"] = traceback
	if complete:
		values["completed_at"] = now_datetime()
	if values:
		frappe.db.set_value("Medusa Sync Log", name, values, update_modified=True)
		frappe.db.commit()


def _as_json(data) -> str | None:
	if data is None:
		return None
	if isinstance(data, str):
		return data
	return json.dumps(data, indent=2, default=str, sort_keys=True)
