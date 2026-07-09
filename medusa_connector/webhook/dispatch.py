# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import now_datetime

# Event handler registry. Later phases register real handlers here, e.g.:
#   "order.placed": "medusa_connector.sync.orders.on_order_placed"
# Each handler receives the Medusa Webhook Log document.
HANDLERS: dict[str, str] = {}


def dispatch_event(log_name: str) -> None:
	"""Background worker: run the handler registered for the log's event, if any."""
	log = frappe.get_doc("Medusa Webhook Log", log_name)
	try:
		handler_path = HANDLERS.get(log.event_name)
		if handler_path:
			frappe.get_attr(handler_path)(log)

		# Phase 1 has no handlers; receipt itself is the outcome.
		log.db_set("status", "Processed", commit=True)
		log.db_set("processed_at", now_datetime(), commit=True)
	except Exception:
		log.db_set("status", "Failed", commit=False)
		log.db_set("error", frappe.get_traceback(), commit=False)
		log.db_set("retry_count", (log.retry_count or 0) + 1, commit=True)
		# Re-raise so the RQ job is recorded as failed and can be retried.
		raise
