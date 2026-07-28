# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe

from medusa_connector.constants import SETTING_DOCTYPE

FIELD_MAP = {
	"length": "medusa_custom_length",
	"width": "medusa_custom_width",
	"height": "medusa_custom_height",
}


class MetadataService:
	"""Apply Medusa product dimensions to ERPNext Item fields."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def apply_product_fields(self, item, mapped: dict) -> bool:
		"""Update product dimension fields and return whether they changed."""
		changed = False

		for key, fieldname in FIELD_MAP.items():
			if not frappe.get_meta("Item").has_field(fieldname):
				continue

			value = mapped.get(key)

			if value in (None, ""):
				value = ""

			if item.get(fieldname) != value:
				item.set(fieldname, value)
				changed = True

		return changed
