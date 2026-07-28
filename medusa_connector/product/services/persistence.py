from __future__ import annotations

import frappe

from medusa_connector.constants import SETTING_DOCTYPE


class PersistenceService:
	"""Reusable document persistence helpers."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def save_item(self, item) -> None:
		"""Persist an Item with Medusa-specific save flags."""
		self.set_item_save_flags(item)
		self.save(item)

	def save(self, doc) -> None:
		"""Insert or save a document."""
		if doc.is_new():
			doc.insert()
			return

		doc.save()

	@staticmethod
	def set_item_save_flags(item) -> None:
		"""Configure Item save flags used during Medusa inbound sync."""
		item.flags.from_medusa = True
		item.flags.from_integration = True
		item.flags.ignore_mandatory = True
		item.flags.ignore_version = True
		item.flags.dont_update_variants = True
