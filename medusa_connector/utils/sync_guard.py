# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Guards for Medusa → ERPNext inbound synchronization."""

from __future__ import annotations

from contextlib import contextmanager

import frappe

INBOUND_FLAG = "medusa_inbound_sync"


def is_inbound_sync() -> bool:
	"""Return True while a Medusa → ERPNext sync is running."""
	return bool(frappe.flags.get(INBOUND_FLAG))


@contextmanager
def inbound_sync():
	"""Mark the current request/job as a Medusa inbound sync."""

	previous = frappe.flags.get(INBOUND_FLAG)

	frappe.flags[INBOUND_FLAG] = True

	try:
		yield
	finally:
		if previous:
			frappe.flags[INBOUND_FLAG] = previous
		else:
			frappe.flags.pop(INBOUND_FLAG, None)
