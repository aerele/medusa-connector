# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Shared Item helpers used by the item/variant sync services and the export flow."""

from __future__ import annotations

import frappe
from ecommerce_core.utils.address_mapping import get_country_name
from frappe.utils import flt, now

from medusa_connector.constants import MODULE_NAME

# Medusa dimension key → ERPNext custom field on the Item doctype.
DIMENSION_FIELDS = {
	"length": "medusa_custom_length",
	"width": "medusa_custom_width",
	"height": "medusa_custom_height",
}


def save_item(item) -> None:
	"""Insert or save an Item with the Medusa inbound-sync save flags."""
	item.flags.from_medusa = True
	item.flags.from_integration = True
	item.flags.ignore_mandatory = True
	item.flags.ignore_version = True
	item.flags.dont_update_variants = True

	if item.is_new():
		item.insert()
	else:
		item.save()


def upsert_mapping(
	*,
	erpnext_item_code: str,
	medusa_product_id: str,
	variant_id: str | None = None,
	sku: str | None = None,
	variant_of: str | None = None,
	has_variants: int = 0,
) -> str:
	"""Create or update the Ecommerce Item mapping.

	``integration_item_code`` always stores the Medusa product id;
	``variant_id`` stores the variant id (empty on template rows). A product
	id alone is not unique — template and variant rows share it — so lookups
	must discriminate on ``has_variants`` / ``variant_id``.
	"""
	has_variants = int(has_variants or 0)

	values = {
		"erpnext_item_code": erpnext_item_code,
		"integration_item_code": medusa_product_id,
		"variant_id": "" if has_variants else (variant_id or ""),
		"sku": None if has_variants else sku,
		"variant_of": "" if has_variants else (variant_of or ""),
		"has_variants": has_variants,
		"item_synced_on": now(),
	}

	name = _existing_mapping_name(erpnext_item_code)

	if name:
		frappe.db.set_value(
			"Ecommerce Item",
			name,
			values,
			update_modified=False,
		)
		return name

	doc = frappe.get_doc(
		{
			"doctype": "Ecommerce Item",
			"integration": MODULE_NAME,
			**values,
		}
	)

	# Unique per call: a shared/module-level name would collide if this ever
	# gets invoked re-entrantly within the same transaction (ROLLBACK TO /
	# RELEASE SAVEPOINT act on the innermost savepoint of a given name).
	savepoint = f"medusa_upsert_mapping_{frappe.generate_hash(length=8)}"

	try:
		frappe.db.savepoint(savepoint)
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Roll back only this insert: a full frappe.db.rollback() would also
		# discard the caller's uncommitted work — e.g. the Item saved just
		# before the mapping in ItemService._sync_item.
		frappe.db.rollback(save_point=savepoint)

		# Escalate to a locking read here (unlike the plain read above): a
		# second concurrent writer could be racing this same recovery path,
		# so we need FOR UPDATE to serialize against it rather than just
		# re-reading a snapshot that might not yet reflect the winner.
		name = _existing_mapping_name(erpnext_item_code, for_update=True)
		if not name:
			raise
		frappe.db.set_value("Ecommerce Item", name, values, update_modified=False)
		return name
	else:
		frappe.db.release_savepoint(savepoint)
		return doc.name


def _existing_mapping_name(erpnext_item_code: str, *, for_update: bool = False) -> str | None:
	if not for_update:
		return frappe.db.get_value(
			"Ecommerce Item",
			{
				"integration": MODULE_NAME,
				"erpnext_item_code": erpnext_item_code,
			},
			"name",
		)

	ecommerce_item = frappe.qb.DocType("Ecommerce Item")
	row = (
		frappe.qb.from_(ecommerce_item)
		.select(ecommerce_item.name)
		.where(
			(ecommerce_item.integration == MODULE_NAME)
			& (ecommerce_item.erpnext_item_code == erpnext_item_code)
		)
		.for_update()
		.run()
	)
	return row[0][0] if row else None


def sync_item_price(item_code: str, mapped: dict, settings) -> None:
	"""Create/update the Item Price on the configured Selling Price List.

	Zero or missing prices are ignored; existing Item Prices are left untouched.
	"""
	price_list = settings.get("price_list")

	if not (price_list and frappe.db.exists("Price List", price_list)):
		return

	rate = mapped.get("standard_rate")

	if rate is None:
		prices = mapped.get("prices") or []
		rate = prices[0].get("amount") if prices else None

	if rate is None:
		return

	rate = flt(rate)

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


def apply_dimension_fields(item, data: dict) -> bool:
	"""Apply Medusa length/width/height to custom Item fields; True when changed."""
	item_meta = frappe.get_meta("Item")

	changed = False

	for source, fieldname in DIMENSION_FIELDS.items():
		if not item_meta.has_field(fieldname):
			continue

		value = data.get(source)

		if value is not None and item.get(fieldname) != value:
			item.set(fieldname, value)
			changed = True

	return changed


def apply_hsn_code(item, hs_code: str | None, *, required_if_india_compliance: bool) -> bool:
	"""Map a Medusa hs_code → Item.gst_hsn_code; throws under India Compliance
	when the code is required but missing, or not in the GST HSN Code master."""
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


def apply_variant_hsn_code(item, variant: dict) -> bool:
	"""Variant HSN with product-level fallback; never clears an existing HSN."""
	variant_hs = str(variant.get("gst_hsn_code") or "").strip()
	product_hs = str(variant.get("product_hs_code") or "").strip()

	hs_code = variant_hs or product_hs

	if not hs_code:
		return False

	return apply_hsn_code(
		item,
		hs_code,
		required_if_india_compliance=True,
	)


def apply_country(item, origin_country: str | None) -> None:
	if not origin_country:
		return
	country = get_country_name(str(origin_country).strip().upper())
	if country:
		item.country_of_origin = country


def ensure_barcodes(item, barcode: str | None = None, ean: str | None = None, upc: str | None = None) -> bool:
	"""Append barcode/EAN/UPC values from the Medusa payload as received;
	duplicates already on this Item or used by another Item are skipped."""
	changed = False
	existing_barcodes = {row.barcode for row in item.barcodes or [] if row.barcode}

	barcode_values = {
		str(value).strip(): barcode_type
		for value, barcode_type in (
			(barcode, None),
			(ean, "EAN"),
			(upc, "UPC-A"),
		)
		if value
	}

	if not barcode_values:
		return False

	existing_barcodes_in_db = {
		row.barcode
		for row in frappe.get_all(
			"Item Barcode",
			filters={"barcode": ["in", list(barcode_values)]},
			fields=["barcode"],
		)
	}

	for barcode_value, barcode_type in barcode_values.items():
		if barcode_value in existing_barcodes or barcode_value in existing_barcodes_in_db:
			continue

		barcode_row = {"barcode": barcode_value}

		if barcode_type:
			barcode_row["barcode_type"] = barcode_type

		item.append("barcodes", barcode_row)
		existing_barcodes.add(barcode_value)
		changed = True

	return changed
