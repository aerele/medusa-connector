# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""ERPNext → Medusa product upload (Item document hooks)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import cint, flt, get_url, now, strip_html

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.medusa.product import ProductService
from medusa_connector.utils.logging import create_sync_log, update_sync_log
from medusa_connector.utils.store_defaults import get_store_defaults
from medusa_connector.utils.sync_guard import is_inbound_sync

DEFAULT_OPTION_TITLE = "Default option"
DEFAULT_OPTION_VALUE = "Default variant"
SYNC_METHOD = "medusa_connector.product.export_products.upload_erpnext_item"


# --------------------------------------------------------------------------- #
# Hook entry point
# --------------------------------------------------------------------------- #
def upload_erpnext_item(doc, method: str | None = None) -> None:
	"""Item after_insert / on_update hook: push to Medusa when settings allow."""
	if not _is_eligible_for_export(doc):
		return

	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.upload_erpnext_items or doc.has_variants:
		return
	if doc.variant_of and not settings.upload_variants_as_items:
		return

	try:
		mapping = _get_mapping(doc.name)
		if mapping:
			if settings.update_medusa_item_on_update:
				_update_medusa_product(doc, mapping)
			return
		_create_medusa_product(doc, settings)
	except Exception:
		frappe.log_error(
			title=f"Medusa upload failed for Item {doc.name}",
			message=frappe.get_traceback(with_context=True),
		)


def _is_eligible_for_export(doc) -> bool:
	flags = doc.flags or {}

	if flags.get("from_medusa") or flags.get("from_integration"):
		return False

	if is_inbound_sync():
		return False

	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return False

	return True


def _get_mapping(item_code: str) -> dict | None:
	return frappe.db.get_value(
		"Ecommerce Item",
		{"integration": MODULE_NAME, "erpnext_item_code": item_code},
		["name", "integration_item_code", "variant_id", "variant_of", "has_variants"],
		as_dict=True,
	)


@contextmanager
def _sync_log(message: str, request_data: Any):
	"""Open a Running sync log and guarantee a Failed log entry on error.

	Callers are responsible for calling `update_sync_log(..., status="Success", ...)`
	themselves once the work succeeds; any unhandled exception is logged as
	Failed here and re-raised.
	"""
	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method=SYNC_METHOD,
		message=message,
		request_data=request_data,
	)
	try:
		yield log_name
	except Exception as exc:
		update_sync_log(
			log_name,
			status="Failed",
			message=str(exc),
			failed=1,
			traceback=frappe.get_traceback(with_context=True),
			complete=True,
		)
		raise


# --------------------------------------------------------------------------- #
# Create flows
# --------------------------------------------------------------------------- #
def _create_medusa_product(item, settings) -> None:
	"""Create a Medusa product, or add a variant to an existing mapped template product."""
	template = frappe.get_doc("Item", item.variant_of) if item.variant_of else item
	service = ProductService()

	if item.variant_of:
		template_product_id = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "erpnext_item_code": template.name, "has_variants": 1},
			"integration_item_code",
		)
		if template_product_id:
			_add_variant_to_product(item, template, template_product_id, settings, service)
			return

	payload = _build_create_payload(item, template, settings)
	with _sync_log(f"Uploading Item {item.name}", payload) as log_name:
		created = service.create_product(payload)
		product_id = created.get("id")
		if not product_id:
			raise frappe.ValidationError("Medusa did not return product id")
		_write_create_mappings(item, template, created)
		update_sync_log(
			log_name,
			status="Success",
			message=f"Uploaded {item.name} → {product_id}",
			created=1,
			complete=True,
		)


def _add_variant_to_product(item, template, product_id: str, settings, service: ProductService) -> None:
	"""Add a new variant to an existing product in Medusa."""
	payload = _variant_payload(item, settings)
	with _sync_log(f"Adding variant {item.name} to {product_id}", payload) as log_name:
		result = service.create_variant(product_id, payload)
		variant_id = result.get("id")
		if not variant_id:
			raise frappe.ValidationError(f"Medusa did not return variant id for Item {item.name}")
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=item.name,
			variant_of=template.name,
			has_variants=0,
		)
		upsert_mapping(
			erpnext_item_code=template.name,
			medusa_product_id=product_id,
			has_variants=1,
		)
		update_sync_log(
			log_name,
			status="Success",
			message=f"Added variant {item.name} → {variant_id}",
			created=1,
			complete=True,
		)


def _write_create_mappings(item, template, created: dict) -> None:
	"""Persist Ecommerce Item mapping(s) for a freshly created Medusa product."""
	product_id = created["id"]
	variants = created.get("variants") or []
	if not variants:
		frappe.throw(f"Medusa product {product_id} has no variant(s)")
	variant_id = variants[0].get("id")
	if not variant_id:
		frappe.throw(f"Medusa product {product_id} has invalid variant response")

	if item.variant_of:
		upsert_mapping(erpnext_item_code=template.name, medusa_product_id=product_id, has_variants=1)

	upsert_mapping(
		erpnext_item_code=item.name,
		medusa_product_id=product_id,
		variant_id=variant_id,
		sku=item.name,
		variant_of=template.name if item.variant_of else None,
		has_variants=0,
	)


# --------------------------------------------------------------------------- #
# Update flow
# --------------------------------------------------------------------------- #
def _update_medusa_product(item, mapping) -> None:
	service = ProductService()
	template = frappe.get_doc("Item", item.variant_of) if item.variant_of else item
	product_payload = _build_update_product_payload(template)
	product_id = mapping.integration_item_code
	variant_id = mapping.get("variant_id")
	is_template = bool(cint(mapping.has_variants))

	if not product_id:
		frappe.throw(f"Cannot resolve Medusa product id for Item {item.name}")
	if not is_template and not variant_id:
		frappe.throw(f"Missing Medusa variant id for Item {item.name}")

	request_data = {"product": product_payload, "item": item.name, "product_id": product_id}
	with _sync_log(f"Updating Item {item.name}", request_data) as log_name:
		service.update_product(product_id, product_payload)
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=None if is_template else variant_id,
			variant_of=None if is_template else (mapping.variant_of or item.variant_of),
			sku=None if is_template else item.name,
			has_variants=mapping.has_variants,
		)
		update_sync_log(
			log_name,
			status="Success",
			message=f"Updated {item.name} → product {product_id}"
			+ (f" variant {variant_id}" if variant_id else ""),
			updated=1,
			complete=True,
		)


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def _build_create_payload(item, template, settings) -> dict:
	is_published = settings.sync_new_item_as_published and not (item.disabled or template.disabled)
	payload = {
		"title": template.item_name or template.name,
		"status": "published" if is_published else "draft",
		"description": _plain_description(template.description),
		"external_id": template.name,
		"metadata": _product_metadata(template, item),
		"options": _template_options(template)
		if item.variant_of
		else [{"title": DEFAULT_OPTION_TITLE, "values": [DEFAULT_OPTION_VALUE]}],
		"variants": [_variant_payload(item, settings)],
	}
	_apply_images(payload, template)
	sales_channel_id = settings.default_sales_channel_id or get_store_defaults().get(
		"default_sales_channel_id"
	)
	if sales_channel_id:
		payload["sales_channels"] = [{"id": sales_channel_id}]
	if flt(template.weight_per_unit):
		payload["weight"] = flt(template.weight_per_unit)
	return payload


def _build_update_product_payload(template) -> dict:
	payload = {
		"title": template.item_name or template.name,
		"description": _plain_description(template.description),
		"external_id": template.name,
		"metadata": _product_metadata(template, template),
	}

	if template.disabled:
		payload["status"] = "draft"
	_apply_images(payload, template)
	if flt(template.weight_per_unit):
		payload["weight"] = flt(template.weight_per_unit)
	return payload


def _variant_payload(item, settings) -> dict:
	options_map = {
		row.attribute: row.attribute_value
		for row in item.attributes or []
		if row.attribute and row.attribute_value
	} or {DEFAULT_OPTION_TITLE: DEFAULT_OPTION_VALUE}

	payload = {
		"title": _variant_title(item),
		"sku": item.name,
		"options": options_map,
		"prices": _variant_prices(item, settings),
		"manage_inventory": _manage_inventory(item, settings),
		"metadata": _variant_metadata(item),
		**_optional_barcode(item),
	}
	if item.get("gst_hsn_code"):
		payload["hs_code"] = item.get("gst_hsn_code")
	if flt(item.weight_per_unit):
		payload["weight"] = flt(item.weight_per_unit)
	return payload


def _apply_images(payload: dict, template) -> None:
	images = _image_payload(template)
	if images:
		payload["images"] = images
		payload["thumbnail"] = images[0]["url"]


# --------------------------------------------------------------------------- #
# Small field-level helpers
# --------------------------------------------------------------------------- #
def _plain_description(description) -> str:
	if not description:
		return ""
	return strip_html(description) if "<" in str(description) else str(description)


def _template_options(template) -> list[dict]:
	attributes = [row.attribute for row in template.attributes or [] if row.attribute]
	if not attributes:
		return [{"title": DEFAULT_OPTION_TITLE, "values": [DEFAULT_OPTION_VALUE]}]

	rows = frappe.get_all(
		"Item Attribute Value",
		filters={"parent": ["in", attributes]},
		fields=["parent", "attribute_value"],
		order_by="idx",
	)
	values_by_attribute: dict[str, list[str]] = {}
	for row in rows:
		values_by_attribute.setdefault(row.parent, []).append(row.attribute_value)

	options = [{"title": attr, "values": values} for attr, values in values_by_attribute.items() if values]
	return options or [{"title": DEFAULT_OPTION_TITLE, "values": [DEFAULT_OPTION_VALUE]}]


def _variant_title(item) -> str:
	attrs = [row.attribute_value for row in (item.attributes or []) if row.attribute_value]
	if attrs:
		return " / ".join(attrs)[:140]
	return (item.item_name or item.name or DEFAULT_OPTION_VALUE)[:140]


def _variant_prices(item, settings) -> list[dict]:
	currency = (settings.default_currency or get_store_defaults().get("default_currency") or "usd").lower()
	amount = flt(item.standard_rate or 0)
	if settings.price_list:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item.name, "price_list": settings.price_list, "selling": 1},
			"price_list_rate",
		)
		if rate is not None:
			amount = flt(rate)
	return [{"currency_code": currency, "amount": amount}]


def _manage_inventory(item, settings) -> bool:
	return bool(settings.update_erpnext_stock_levels_to_medusa and item.is_stock_item)


def _optional_barcode(item) -> dict:
	barcodes = item.barcodes or []
	return {"barcode": barcodes[0].barcode} if barcodes and barcodes[0].barcode else {}


def _product_metadata(template, item) -> dict:
	meta = {
		"erpnext_item_code": template.name,
		"erpnext_item_group": template.item_group or "",
		"source": "erpnext",
	}
	if item.name != template.name:
		meta["erpnext_variant_item_code"] = item.name
	return meta


def _variant_metadata(item) -> dict:
	return {"erpnext_item_code": item.name, "source": "erpnext"}


def _image_payload(item) -> list[dict]:
	url = _absolute_image_url(item.image)
	return [{"url": url}] if url else []


def _absolute_image_url(image: str | None) -> str | None:
	if not image:
		return None
	image = str(image).strip()
	if image.startswith(("http://", "https://")):
		return image
	if image.startswith("/"):
		return get_url(image)
	if image.startswith(("files/", "private/files/")):
		return get_url("/" + image)
	return None


# --------------------------------------------------------------------------- #
# Mapping persistence
# --------------------------------------------------------------------------- #
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

	Ecommerce Item is the single source of truth; this function only writes
	mappings created by the Medusa sync. Only fields that actually exist on
	the Ecommerce Item doctype (integration, erpnext_item_code,
	integration_item_code, sku, has_variants, variant_id, variant_of,
	item_synced_on) are written.
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
	filters = {
		"integration": MODULE_NAME,
		"integration_item_code": medusa_product_id,
		"has_variants": has_variants,
	}
	# Variant rows are unique by product + variant id; template rows are unique by product alone.
	if not has_variants:
		filters["variant_id"] = variant_id or ""

	name = frappe.db.exists("Ecommerce Item", filters)
	if name:
		frappe.db.set_value("Ecommerce Item", name, values, update_modified=False)
		return name

	doc = frappe.get_doc({"doctype": "Ecommerce Item", "integration": MODULE_NAME, **values})
	doc.insert(ignore_permissions=True)
	return doc.name
