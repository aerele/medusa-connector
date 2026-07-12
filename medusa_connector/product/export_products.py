# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""ERPNext → Medusa product upload (mirrors Shopify upload flags)."""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from medusa_connector.constants import SETTING_DOCTYPE
from medusa_connector.medusa.product import ProductService
from medusa_connector.medusa_connector.doctype.medusa_item_mapping.medusa_item_mapping import (
	get_erpnext_item_code,
	is_synced,
	upsert_mapping,
)
from medusa_connector.medusa_connector.doctype.medusa_sync_log.medusa_sync_log import (
	create_sync_log,
	update_sync_log,
)


def upload_erpnext_item(doc, method: str | None = None) -> None:
	"""Item after_insert / on_update hook: push to Medusa when settings allow."""
	if getattr(doc, "flags", None) and (doc.flags.get("from_medusa") or doc.flags.get("from_integration")):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return

	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.get("upload_erpnext_items"):
		return

	# Templates are not uploaded directly; upload is driven by non-template items
	# (simple items or variants when upload_variants_as_items is on).
	if doc.has_variants:
		return

	if doc.variant_of and not settings.get("upload_variants_as_items"):
		return

	try:
		if _mapping_exists_for_item(doc.name):
			if settings.get("update_medusa_item_on_update"):
				_update_medusa_product(doc, settings)
			return
		_create_medusa_product(doc, settings)
	except Exception:
		frappe.log_error(
			title=f"Medusa upload failed for Item {doc.name}",
			message=frappe.get_traceback(with_context=True),
		)


def _mapping_exists_for_item(item_code: str) -> bool:
	return bool(frappe.db.exists("Medusa Item Mapping", {"erpnext_item_code": item_code}))


def _create_medusa_product(item, settings) -> None:
	template = frappe.get_doc("Item", item.variant_of) if item.variant_of else item
	service = ProductService()

	status = "published" if settings.get("sync_new_item_as_published") else "draft"
	payload: dict = {
		"title": template.item_name or template.name,
		"status": status,
		"description": template.description or "",
	}

	if item.variant_of:
		payload["options"] = _template_options(template)
		payload["variants"] = [_variant_payload(item, template)]
	else:
		payload["options"] = [{"title": "Default option", "values": ["Default variant"]}]
		payload["variants"] = [
			{
				"title": "Default variant",
				"sku": item.name,
				"options": {"Default option": "Default variant"},
				"manage_inventory": False,
			}
		]

	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method="medusa_connector.product.export_products.upload_erpnext_item",
		message=f"Uploading Item {item.name}",
		request_data=payload,
	)
	try:
		created = service.create_product(payload)
		product_id = created.get("id")
		if not product_id:
			raise frappe.ValidationError("Medusa did not return a product id")

		# Map template / simple item
		if item.variant_of:
			upsert_mapping(
				erpnext_item_code=template.name,
				medusa_product_id=product_id,
				has_variants=1,
			)
			variant_id = _match_variant_id(created, item)
			upsert_mapping(
				erpnext_item_code=item.name,
				medusa_product_id=product_id,
				variant_id=variant_id,
				sku=item.name,
				variant_of=template.name,
				has_variants=0,
			)
		else:
			variant_id = None
			variants = created.get("variants") or []
			if variants:
				variant_id = variants[0].get("id")
			upsert_mapping(
				erpnext_item_code=item.name,
				medusa_product_id=product_id,
				variant_id=variant_id,
				sku=item.name,
				has_variants=0,
			)

		update_sync_log(
			log_name,
			status="Success",
			message=f"Uploaded {item.name} → {product_id}",
			created=1,
			response_data=created,
			complete=True,
		)
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


def _update_medusa_product(item, settings) -> None:
	row = frappe.db.get_value(
		"Medusa Item Mapping",
		{"erpnext_item_code": item.name},
		["medusa_product_id", "medusa_variant_id", "variant_of"],
		as_dict=True,
	)
	if not row:
		return
	service = ProductService()
	payload = {
		"title": item.item_name or item.name,
		"description": item.description or "",
	}
	if item.disabled:
		payload["status"] = "draft"
	service.update_product(row.medusa_product_id, payload)
	upsert_mapping(
		erpnext_item_code=item.name,
		medusa_product_id=row.medusa_product_id,
		variant_id=row.medusa_variant_id,
		variant_of=row.variant_of,
		sku=item.name,
		has_variants=0,
		status="Disabled" if item.disabled else "Active",
	)


def _template_options(template) -> list[dict]:
	options = []
	for row in template.attributes or []:
		attr = frappe.get_doc("Item Attribute", row.attribute)
		values = [v.attribute_value for v in attr.item_attribute_values or [] if v.attribute_value]
		if values:
			options.append({"title": row.attribute, "values": values})
	if not options:
		options = [{"title": "Default option", "values": ["Default variant"]}]
	return options


def _variant_payload(item, template) -> dict:
	options = {row.attribute: row.attribute_value for row in item.attributes or []}
	return {
		"title": item.item_name or item.name,
		"sku": item.name,
		"options": options,
		"manage_inventory": False,
	}


def _match_variant_id(product: dict, item) -> str | None:
	"""Best-effort match of Medusa variant by option values or SKU."""
	wanted = {row.attribute: row.attribute_value for row in item.attributes or []}
	for variant in product.get("variants") or []:
		if variant.get("sku") == item.name:
			return variant.get("id")
		# option values may be a list of {option_id, value} or a map
		opts = variant.get("options") or []
		if isinstance(opts, dict) and opts == wanted:
			return variant.get("id")
	variants = product.get("variants") or []
	return variants[0].get("id") if variants else None
