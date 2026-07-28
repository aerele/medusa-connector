from __future__ import annotations

import frappe
from frappe.utils import flt

from medusa_connector.constants import SETTING_DOCTYPE


class PriceService:
	"""Synchronize ERPNext Item Price from Medusa prices."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def sync_item_price(self, item_code: str, mapped: dict) -> None:
		"""Create or update the configured Selling Price List.

		Zero or missing prices are ignored. Existing Item Prices are not
		changed or deleted when the incoming Medusa price is zero.
		"""
		price_list = self.settings.get("price_list")

		if not (price_list and frappe.db.exists("Price List", price_list)):
			return

		rate = mapped.get("standard_rate")

		if rate is None:
			prices = mapped.get("prices") or []
			rate = prices[0].get("amount") if prices else None

		if rate is None:
			return

		rate = flt(rate)

		# Do not create or update Item Price when the
		# Medusa price is zero or negative.
		if rate <= 0:
			return

		existing = frappe.db.get_value(
			"Item Price",
			{
				"item_code": item_code,
				"price_list": price_list,
				"selling": 1,
			},
			[
				"name",
				"price_list_rate",
			],
			as_dict=True,
		)

		if existing:
			if flt(existing.price_list_rate) != rate:
				frappe.db.set_value(
					"Item Price",
					existing.name,
					"price_list_rate",
					rate,
					update_modified=False,
				)
			return

		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate,
				"selling": 1,
			}
		).insert()
