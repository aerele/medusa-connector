from __future__ import annotations

import frappe
from ecommerce_core.utils.address_mapping import get_country_name

from medusa_connector.constants import SETTING_DOCTYPE


class HSNService:
	"""Handles HSN, country of origin and barcode mapping."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def apply_hsn_code(self, item, hs_code: str | None, *, required_if_india_compliance: bool) -> bool:
		"""Map a resolved Medusa hs_code → Item.gst_hsn_code (India Compliance).

		Rules:
		- IC not installed → no-op (allow create) unless a code is provided and field exists.
		- IC installed + HS missing + required → raise ValidationError.
		- IC installed + HS not in GST HSN Code master → raise ValidationError.
		- IC installed + HS exists in master → set on Item.
		"""
		if not frappe.get_meta("Item").has_field("gst_hsn_code"):
			return False

		hs_code = str(hs_code or "").strip()
		if not hs_code:
			if required_if_india_compliance and "india_compliance" in frappe.get_installed_apps():
				frappe.throw(
					frappe._(
						"HS / HSN code is required for Item sync when India Compliance is installed. "
						"Set hs_code on the Medusa product (default) and/or the product variant."
					),
					title=frappe._("Missing HSN Code"),
				)
			return False

		if "india_compliance" in frappe.get_installed_apps() and frappe.db.exists("DocType", "GST HSN Code"):
			if not frappe.db.exists("GST HSN Code", hs_code):
				frappe.throw(
					frappe._(
						"Medusa HS code '{0}' was not found in GST HSN Code. "
						"Add it under GST HSN Code or correct the product in Medusa."
					).format(hs_code),
					title=frappe._("Invalid HSN Code"),
				)

		if item.get("gst_hsn_code") == hs_code:
			return False
		item.gst_hsn_code = hs_code
		return True

	def apply_variant_hsn_code(self, item, variant: dict) -> bool:
		"""Apply the effective HSN code for a Medusa variant.

		Variant HSN takes priority. If the variant has no HSN,
		the product-level HSN is used as the fallback.

		The ERPNext variant should never clear its HSN merely because
		the variant payload does not contain a variant-level HSN.
		"""
		variant_hs = str(variant.get("gst_hsn_code") or "").strip()
		product_hs = str(variant.get("product_hs_code") or "").strip()

		hs_code = variant_hs or product_hs

		if not hs_code:
			return False

		return self.apply_hsn_code(
			item,
			hs_code,
			required_if_india_compliance=True,
		)

	@staticmethod
	def apply_country(item, origin_country: str | None) -> None:
		if not origin_country:
			return
		country = get_country_name(str(origin_country).strip().upper())
		if country:
			item.country_of_origin = country

	@staticmethod
	def ensure_barcode(item, barcode: str) -> None:
		barcode = str(barcode).strip()
		if not barcode:
			return
		existing = {row.barcode for row in item.barcodes or []}
		if barcode not in existing:
			item.append("barcodes", {"barcode": barcode})
