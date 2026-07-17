# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Catalog metadata → ERPNext Item custom fields.

Writes directly to real Item fields — never parses or rewrites the
description text. If a target custom field doesn't exist on this site yet,
the corresponding value is skipped (checked via has_field), not guessed at
or recovered from anywhere else.

Requires these custom fields on Item (create via Customize Form or a
fixtures file — not created by this connector):
    custom_medusa_handle, custom_medusa_product_type, custom_medusa_collection,
    custom_material, custom_mid_code, custom_length, custom_width, custom_height
"""

from __future__ import annotations

import frappe

from medusa_connector.constants import SETTING_DOCTYPE

FIELD_MAP = {
	"handle": "custom_medusa_handle",
	"product_type": "custom_medusa_product_type",
	"collection": "custom_medusa_collection",
	"material": "custom_material",
	"mid_code": "custom_mid_code",
	"length": "custom_length",
	"width": "custom_width",
	"height": "custom_height",
}


class MetadataService:
	"""Applies Medusa catalog metadata onto ERPNext Item custom fields."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def apply_product_fields(self, item, mapped: dict) -> bool:
		"""Set catalog metadata fields from a mapped Medusa product (product sync).

		Trusts the mapper's output as-is: if Medusa has no value for a field,
		the mapped dict carries None and the Item field is cleared to match —
		Medusa is the source of truth, so ERPNext should reflect it exactly.
		Returns True if any field was actually changed.
		"""
		return any(self._set(item, key, mapped.get(key)) for key in FIELD_MAP)

	def apply_inventory_fields(self, item, inv: dict) -> bool:
		"""Set catalog metadata fields from a Medusa Inventory Item payload.

		Returns True if any field was actually changed — callers that only
		save on a detected change (e.g. the inventory-item webhook) rely on
		this, since these fields live outside the native-field snapshot
		they diff against.
		"""
		return any(
			self._set(item, key, inv.get(key))
			for key in ("material", "mid_code", "length", "width", "height")
		)

	def _set(self, item, key: str, value) -> bool:
		fieldname = FIELD_MAP.get(key)
		if not fieldname or not frappe.get_meta("Item").has_field(fieldname):
			return False
		value = value if value not in (None, "") else ""
		if item.get(fieldname) == value:
			return False
		item.set(fieldname, value)
		return True
