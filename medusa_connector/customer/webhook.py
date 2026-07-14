# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Customer webhook events — acknowledged only (no ERPNext customer create).

Shopify / Ecommerce Core pattern: customers are created when **orders** sync,
not on standalone customer.created/updated webhooks.

Events stay registered so Medusa subscriptions remain complete; handlers are
no-ops for create/update. Optional soft-disable on delete keeps ERPNext tidy.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from medusa_connector.constants import CUSTOMER_ID_FIELD
from medusa_connector.webhook.base import BaseHandler, MedusaEvent
from medusa_connector.webhook.registry import register


@register("customer.created", "customer.updated")
class CustomerHandler(BaseHandler):
	"""No instant customer sync — wait for order pipeline."""

	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		customer_id = event.entity_id or (entity or {}).get("id")
		return (
			f"{event.name}: acknowledged {customer_id or '-'} "
			"(customer create/update runs at order sync only)"
		)


@register("customer.deleted")
class CustomerDeletedHandler(BaseHandler):
	"""Soft-disable ERPNext Customer if previously created via an order."""

	resource = None

	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		customer_id = cstr(event.entity_id or (entity or {}).get("id") or "")
		if not customer_id:
			return "customer.deleted: missing id"

		name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")
		if not name:
			return f"customer.deleted: no ERPNext Customer for {customer_id}"

		doc = frappe.get_doc("Customer", name)
		if not doc.disabled:
			doc.disabled = 1
			doc.flags.ignore_mandatory = True
			doc.save(ignore_permissions=True)
			return f"customer.deleted: disabled {name}"
		return f"customer.deleted: already disabled {name}"
