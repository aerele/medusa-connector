# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import get_url


class MedusaSettings(Document):
	def validate(self) -> None:
		# Surface the guest receiver URL so the admin can paste it into Medusa.
		self.webhook_receiver_url = get_url("/api/method/medusa_connector.api.webhook.receive")


@frappe.whitelist()
def test_connection() -> dict:
	"""Run a health query against Medusa and refresh the stored connection status."""
	from medusa_connector.medusa.client import test_connection as _test_connection

	return _test_connection()


@frappe.whitelist()
def regenerate_webhook_secret() -> str:
	"""Generate a fresh HMAC secret. Invalidates the current Medusa subscriber secret."""
	doc = frappe.get_single("Medusa Settings")
	doc.webhook_secret = secrets.token_urlsafe(32)
	doc.save()
	frappe.db.commit()
	return frappe._("Webhook secret regenerated. Copy it into your Medusa subscriber.")
