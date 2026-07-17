# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MedusaWebhookRegistration(Document):
	"""Child row mirroring one Medusa webhook subscription managed by the connector.

	Rows are written exclusively by ``WebhookSyncService``; they are a read-only
	audit surface inside Medusa Settings and are never edited by hand.
	"""

	pass
