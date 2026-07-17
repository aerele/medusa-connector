# ------------------------------------------------------------------
# Masters
# ------------------------------------------------------------------
from __future__ import annotations

import frappe
from frappe.utils import cstr
from frappe.utils.nestedset import get_root_of

from medusa_connector.constants import DEFAULT_ITEM_GROUP, DEFAULT_STOCK_UOM, SETTING_DOCTYPE


class MasterService:
	"""Creates and reuses ERPNext master data (UOM, Item Group, Brand, Item Attribute)."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	# Shared get-or-create
	@staticmethod
	def get_or_create(doctype: str, name: str, name_field: str, extra: dict | None = None) -> str | None:
		"""Return an existing master record or create it if missing."""
		name = (name or "").strip()
		if not name:
			return None
		if frappe.db.exists(doctype, name):
			return name
		doc = {"doctype": doctype, name_field: name}
		if extra:
			doc.update(extra)
		frappe.get_doc(doc).insert(ignore_permissions=True)
		return name

	# UOM
	def ensure_stock_uom(self, uom: str | None) -> str:
		return self.get_or_create(
			"UOM", uom or self.settings.default_stock_uom or DEFAULT_STOCK_UOM, "uom_name"
		)

	# Item Group
	def ensure_item_group(self, mapped: dict) -> str:
		"""Resolve/create Item Group from categories (hierarchy) or mapped name."""
		categories = mapped.get("categories") or []
		# Prefer deepest category with parent when auto-creating hierarchy.
		if categories:
			for cat in categories:
				name = cat.get("name")
				if not name:
					continue
				parent_name = cat.get("parent_name")
				return self.ensure_group(name, parent_name)
		name = mapped.get("item_group") or self.settings.get("item_group") or DEFAULT_ITEM_GROUP
		return self.ensure_group(name, None)

	def ensure_group(self, name: str, parent_name: str | None) -> str:
		name = (name or DEFAULT_ITEM_GROUP).strip()
		# Check first, before resolving the parent — avoids an unnecessary
		# recursive ensure_group()/get_root_of() call when the group
		# already exists.
		if frappe.db.exists("Item Group", name):
			return name
		parent = self.ensure_group(parent_name, None) if parent_name else get_root_of("Item Group")
		return self.get_or_create(
			"Item Group",
			name,
			"item_group_name",
			extra={"parent_item_group": parent, "is_group": 0},
		)

	# Brand
	def ensure_brand(self, brand: str | None) -> str | None:
		return self.get_or_create("Brand", brand, "brand")

	# Item Attribute
	def sync_attributes(self, attributes: list[dict]) -> None:
		for attribute in attributes:
			name = attribute.get("name")
			if not name:
				continue
			values = list(dict.fromkeys(v for v in (attribute.get("values") or []) if v))
			if not values:
				continue

			if not frappe.db.exists("Item Attribute", name):
				used: set[str] = set()
				self.get_or_create(
					"Item Attribute",
					name,
					"attribute_name",
					extra={
						"item_attribute_values": [
							{
								"attribute_value": value,
								"abbr": self.unique_abbr(value, used),
							}
							for value in values
						]
					},
				)
				continue

			# Skip numeric attributes
			if frappe.db.get_value("Item Attribute", name, "numeric_values"):
				continue

			rows = frappe.get_all(
				"Item Attribute Value",
				filters={"parent": name},
				fields=["attribute_value", "abbr"],
			)
			existing_values = {row.attribute_value for row in rows}
			existing_abbrs = {row.abbr for row in rows}
			new_values = [value for value in values if value not in existing_values]
			if not new_values:
				continue

			item_attribute = frappe.get_doc("Item Attribute", name)
			for value in new_values:
				item_attribute.append(
					"item_attribute_values",
					{
						"attribute_value": value,
						"abbr": self.unique_abbr(value, existing_abbrs),
					},
				)
			item_attribute.save()

	@staticmethod
	def unique_abbr(value: str, used: set[str]) -> str:
		base = cstr(value)[:10] or "VAL"
		abbr = base
		idx = 1
		while abbr in used:
			suffix = str(idx)
			abbr = f"{base[: max(1, 10 - len(suffix))]}{suffix}"
			idx += 1
		used.add(abbr)
		return abbr
