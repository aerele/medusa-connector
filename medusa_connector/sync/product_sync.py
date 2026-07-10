"""Synchronise mapped Medusa products to ERPNext Items."""

import frappe


class ProductSync:
	def sync(self, mapped_product: dict) -> str:
		"""Create/update the template, its option attributes, and its variants."""
		self._sync_attributes(mapped_product.get("attributes") or [])
		item_data = {
			key: value for key, value in mapped_product.items() if key not in {"attributes", "variants"}
		}
		item_data["attributes"] = [
			{"attribute": attribute["name"]}
			for attribute in mapped_product.get("attributes") or []
			if attribute["name"]
		]
		item_code = mapped_product["item_code"]
		if frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			item.update(item_data)
			item.save()
		else:
			item = frappe.get_doc({"doctype": "Item", **item_data})
			item.insert()

		for variant in mapped_product.get("variants") or []:
			self._sync_variant(item, variant)
		return item.name

	def _sync_attributes(self, attributes: list[dict]) -> None:
		for attribute in attributes:
			name = attribute["name"]
			values = attribute.get("values") or []
			if frappe.db.exists("Item Attribute", name):
				item_attribute = frappe.get_doc("Item Attribute", name)
				existing = {row.attribute_value for row in item_attribute.item_attribute_values}
				for value in values:
					if value not in existing:
						item_attribute.append(
							"item_attribute_values", {"attribute_value": value, "abbr": value[:10]}
						)
				item_attribute.save()
			else:
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": name,
						"item_attribute_values": [
							{"attribute_value": value, "abbr": value[:10]} for value in values
						],
					}
				).insert()

	def _sync_variant(self, template, variant: dict) -> None:
		variant_id = variant.get("item_code")
		if not variant_id:
			return
		data = {
			"item_code": variant_id,
			"item_name": variant["item_name"],
			"variant_of": template.name,
			"variant_based_on": "Item Attribute",
			"stock_uom": template.stock_uom,
			"image": variant.get("image"),
			"attributes": [
				{"attribute": name, "attribute_value": value}
				for name, value in variant.get("attributes", {}).items()
			],
		}
		if variant.get("barcode"):
			data["barcodes"] = [{"barcode": variant["barcode"]}]
		if frappe.db.exists("Item", variant_id):
			item = frappe.get_doc("Item", variant_id)
			item.update(data)
			item.save()
		else:
			frappe.get_doc({"doctype": "Item", **data}).insert()
