# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""ERPNext → Medusa product upload (Item document hooks)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import flt, strip_html

from medusa_connector.constants import (
	DEFAULT_OPTION_VALUE,
	DEFAULT_VARIANT_TITLE,
	MODULE_NAME,
	SETTING_DOCTYPE,
)
from medusa_connector.medusa.product import ProductService
from medusa_connector.product.services.utils import upsert_mapping
from medusa_connector.utils.logging import create_sync_log, update_sync_log
from medusa_connector.utils.store_defaults import get_store_defaults
from medusa_connector.utils.sync_guard import is_inbound_sync

DEFAULT_OPTION_TITLE = "Default option"
RETRY_METHOD = "medusa_connector.product.export_products.retry_erpnext_item_upload"
UPLOAD_JOB = "medusa_connector.product.export_products.upload_item_to_medusa"


def upload_erpnext_item(doc, method: str | None = None) -> None:
	"""Item after_insert / on_update hook: push new Items to Medusa.

	Medusa is the item master — creations push outbound, while updates to
	already-synced Items only flow inbound (Medusa → ERPNext). The upload is
	enqueued after commit so Medusa's creation webhooks cannot race ahead of
	the Item's own transaction.
	"""
	if not _is_eligible_for_export(doc):
		return

	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.upload_erpnext_items or doc.has_variants:
		return
	if doc.variant_of and not settings.upload_variants_as_items:
		return

	if _get_mapping(doc.name):
		return

	frappe.enqueue(
		UPLOAD_JOB,
		queue="short",
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"medusa_upload:{doc.name}",
		item_code=doc.name,
	)


def upload_item_to_medusa(item_code: str) -> None:
	"""Background job: push a committed, not-yet-synced Item to Medusa."""
	if _get_mapping(item_code):
		return

	try:
		item = frappe.get_doc("Item", item_code)
		settings = frappe.get_cached_doc(SETTING_DOCTYPE)
		_create_medusa_product(item, settings)
	except Exception:
		frappe.log_error(
			title=f"Medusa upload failed for Item {item_code}",
			message=frappe.get_traceback(with_context=True),
		)


def retry_erpnext_item_upload(payload: dict, request_id: str | None = None) -> None:
	"""Retry a failed product export from the integration log Retry button."""
	if not payload:
		frappe.throw("Payload is required for retry")

	# Product payloads carry external_id; variant payloads only metadata.
	item_code = payload.get("external_id") or (payload.get("metadata") or {}).get("erpnext_item_code")
	if not item_code:
		frappe.throw("Cannot determine ERPNext item code from payload")

	if not frappe.db.exists("Item", item_code):
		frappe.throw(f"Item {item_code} not found in ERPNext")

	item = frappe.get_doc("Item", item_code)
	settings = frappe.get_cached_doc(SETTING_DOCTYPE)

	try:
		if _get_mapping(item_code):
			if request_id:
				update_sync_log(
					request_id,
					status="Success",
					message=f"Item {item_code} is already synced",
					complete=True,
				)
			return
		_create_medusa_product(item, settings)
	except Exception as exc:
		if request_id:
			update_sync_log(
				request_id,
				status="Failed",
				message=str(exc),
				traceback=frappe.get_traceback(with_context=True),
				complete=True,
			)
		raise

	if request_id:
		update_sync_log(
			request_id,
			status="Success",
			message=f"Retried upload of Item {item_code}",
			complete=True,
		)


def _is_eligible_for_export(doc) -> bool:
	flags = doc.flags or {}

	if flags.get("from_medusa") or flags.get("from_integration"):
		return False

	if is_inbound_sync():
		return False

	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install or frappe.flags.in_test:
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
	"""Open a Running log, mark it Failed and re-raise on error; callers set Success."""
	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method=RETRY_METHOD,
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
		product = service.create_variant(product_id, payload)
		variant_id = _created_variant_id(product, payload)
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


def _created_variant_id(product: dict, payload: dict) -> str | None:
	"""create_variant returns the updated parent product; match the new variant by sent SKU."""
	if not product.get("variants"):
		variant_id = product.get("id")
		return variant_id if str(variant_id or "").startswith("variant_") else None

	sku = payload.get("sku")
	return next((v.get("id") for v in product["variants"] if sku and v.get("sku") == sku), None)


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
	sales_channel_id = settings.default_sales_channel_id or get_store_defaults().get(
		"default_sales_channel_id"
	)
	if sales_channel_id:
		payload["sales_channels"] = [{"id": sales_channel_id}]
	if flt(template.weight_per_unit):
		payload["weight"] = flt(template.weight_per_unit)
	return payload


def _variant_payload(item, settings) -> dict:
	"""Create payload — Medusa requires ``prices`` on create; updates never send prices."""
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
		**_barcode_payload(item),
	}
	if flt(item.weight_per_unit):
		payload["weight"] = flt(item.weight_per_unit)
	return payload


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
	# Simple products match Medusa Admin's native default variant title
	return DEFAULT_VARIANT_TITLE


def _variant_prices(item, settings) -> list[dict]:
	if not settings.price_list:
		return []

	price_row = frappe.db.get_value(
		"Item Price",
		{"item_code": item.name, "price_list": settings.price_list, "selling": 1},
		["price_list_rate", "currency"],
		as_dict=True,
	)
	if not price_row or price_row.price_list_rate is None or not price_row.currency:
		return []

	return [
		{
			"currency_code": price_row.currency.lower(),
			"amount": flt(price_row.price_list_rate),
		}
	]


def _manage_inventory(item, settings) -> bool:
	return bool(settings.update_erpnext_stock_levels_to_medusa and item.is_stock_item)


def _barcode_payload(item) -> dict:
	"""First barcode, sent on variant creation only (never updated)."""
	for row in item.barcodes or []:
		barcode = str(row.barcode or "").strip()
		if barcode:
			return {"barcode": barcode}
	return {}


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
