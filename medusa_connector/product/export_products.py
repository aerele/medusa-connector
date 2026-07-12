# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""ERPNext → Medusa product upload (Shopify-style Item hooks).

Medusa Admin API (verified against OpenAPI / docs.medusajs.com/api/admin):

- ``POST /admin/products`` — AdminCreateProduct
  - required: ``title``
  - ``options[].values``: string[]
  - ``variants[]``: AdminCreateProductVariant — required ``title``, ``prices``
  - ``variants[].options``: object map ``{OptionTitle: value}``
  - ``variants[].prices``: ``[{currency_code, amount}]`` (major units)
  - optional: ``status``, ``description``, ``images[{url}]``, ``sales_channels[{id}]``,
    ``metadata``, ``external_id``, ``categories[{id}]``, ``tags[{id}]``, ``collection_id``
- ``POST /admin/products/{id}`` — AdminUpdateProduct
- ``POST /admin/products/{id}/variants`` — create variant
- ``POST /admin/products/{id}/variants/{variant_id}`` — update variant
- ``DELETE /admin/products/{id}`` — delete product

Architecture mirrors Shopify ``upload_erpnext_item``: gated by settings flags,
driven by leaf Item create/update (not template), mapping + sync log.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt, get_url, strip_html

from medusa_connector.constants import SETTING_DOCTYPE
from medusa_connector.medusa.product import ProductService
from medusa_connector.medusa_connector.doctype.medusa_item_mapping.medusa_item_mapping import (
	upsert_mapping,
)
from medusa_connector.medusa_connector.doctype.medusa_sync_log.medusa_sync_log import (
	create_sync_log,
	update_sync_log,
)
from medusa_connector.utils.store_defaults import get_store_defaults

DEFAULT_OPTION_TITLE = "Default option"
DEFAULT_OPTION_VALUE = "Default variant"


def upload_erpnext_item(doc, method: str | None = None) -> None:
	"""Item after_insert / on_update hook: push to Medusa when settings allow."""
	from medusa_connector.utils.sync_guard import should_skip_export_for_item

	if getattr(doc, "flags", None) and (doc.flags.get("from_medusa") or doc.flags.get("from_integration")):
		return
	# Global inbound flag + short cache after webhook import (covers ERPNext's
	# update_variants cascade which re-saves variants without doc flags).
	if should_skip_export_for_item(getattr(doc, "name", None)):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return

	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.get("upload_erpnext_items"):
		return

	# Templates are not uploaded directly (Shopify pattern); leaf items drive upload.
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


def archive_erpnext_item(doc, method: str | None = None) -> None:
	"""Item on_trash: draft or delete the linked Medusa product (settings-gated)."""
	from medusa_connector.utils.sync_guard import should_skip_export_for_item

	if getattr(doc, "flags", None) and (doc.flags.get("from_medusa") or doc.flags.get("from_integration")):
		return
	if should_skip_export_for_item(getattr(doc, "name", None)):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_install:
		return

	settings = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.enabled or not settings.get("upload_erpnext_items"):
		return

	row = frappe.db.get_value(
		"Medusa Item Mapping",
		{"erpnext_item_code": doc.name},
		["name", "medusa_product_id", "medusa_variant_id", "has_variants", "variant_of"],
		as_dict=True,
	)
	if not row or not row.medusa_product_id:
		return

	service = ProductService()
	action = settings.get("on_item_delete") or "Draft"
	try:
		if row.variant_of and row.medusa_variant_id and not cint(row.has_variants):
			# Leaf variant: remove only the Medusa variant when possible.
			if action == "Delete":
				service.delete_variant(row.medusa_product_id, row.medusa_variant_id)
			else:
				service.update_variant(
					row.medusa_product_id,
					row.medusa_variant_id,
					{"metadata": {"erpnext_status": "deleted"}},
				)
			frappe.db.set_value("Medusa Item Mapping", row.name, "status", "Orphaned")
			return

		if action == "Delete":
			service.delete_product(row.medusa_product_id)
		else:
			service.update_product(row.medusa_product_id, {"status": "draft"})
		frappe.db.set_value(
			"Medusa Item Mapping",
			{"medusa_product_id": row.medusa_product_id},
			"status",
			"Orphaned",
			update_modified=False,
		)
	except Exception:
		frappe.log_error(
			title=f"Medusa archive failed for Item {doc.name}",
			message=frappe.get_traceback(with_context=True),
		)


def _mapping_exists_for_item(item_code: str) -> bool:
	return bool(frappe.db.exists("Medusa Item Mapping", {"erpnext_item_code": item_code}))


def _create_medusa_product(item, settings) -> None:
	"""Create a Medusa product, or add a variant to an existing mapped template product."""
	template = frappe.get_doc("Item", item.variant_of) if item.variant_of else item
	service = ProductService()

	# Multi-variant: if the template already maps to a Medusa product, add a variant.
	if item.variant_of:
		template_map = frappe.db.get_value(
			"Medusa Item Mapping",
			{"erpnext_item_code": template.name, "has_variants": 1},
			["medusa_product_id"],
			as_dict=True,
		)
		if template_map and template_map.medusa_product_id:
			_add_variant_to_product(item, template, template_map.medusa_product_id, settings, service)
			return

	payload = _build_create_payload(item, template, settings, service)

	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method="medusa_connector.product.export_products.upload_erpnext_item",
		message=f"Uploading Item {item.name}",
		request_data=payload,
	)
	try:
		created = service.create_product(payload)
		# create may return the product only — re-fetch for full variants/prices if needed
		product_id = created.get("id")
		if not product_id:
			raise frappe.ValidationError("Medusa did not return a product id")
		if not created.get("variants"):
			created = service.get_product(product_id)

		from medusa_connector.utils.sync_guard import mark_product_exported

		mark_product_exported(product_id)
		_write_create_mappings(item, template, created)
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


def _add_variant_to_product(item, template, product_id: str, settings, service: ProductService) -> None:
	"""``POST /admin/products/{id}/variants`` for an additional ERPNext variant."""
	# Ensure product options cover this variant's attributes.
	product = service.get_product(product_id)
	_ensure_product_options(service, product_id, product, template, item)

	payload = _variant_payload(item, template, settings=settings)
	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method="medusa_connector.product.export_products.upload_erpnext_item",
		message=f"Adding variant {item.name} to {product_id}",
		request_data=payload,
	)
	try:
		# API returns the product (with variants) per Admin docs / common Medusa shape.
		result = service.create_variant(product_id, payload)
		product = result if result.get("variants") else service.get_product(product_id)
		from medusa_connector.utils.sync_guard import mark_product_exported

		mark_product_exported(product_id)
		variant_id = _match_variant_id(product, item)
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=item.name,
			variant_of=template.name,
			has_variants=0,
		)
		# Keep template mapping active.
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
			response_data=product,
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
		["name", "medusa_product_id", "medusa_variant_id", "variant_of", "has_variants"],
		as_dict=True,
	)
	if not row:
		return

	service = ProductService()
	template = frappe.get_doc("Item", item.variant_of) if item.variant_of else item
	product_payload = _build_update_product_payload(template if item.variant_of else item, settings)

	log_name = create_sync_log(
		sync_type="Product Export",
		status="Running",
		method="medusa_connector.product.export_products.upload_erpnext_item",
		message=f"Updating Item {item.name}",
		request_data={"product": product_payload, "item": item.name},
	)
	try:
		updated = service.update_product(row.medusa_product_id, product_payload)

		# Update the sellable variant (simple item or ERPNext variant).
		if not cint(row.has_variants):
			variant_id = row.medusa_variant_id
			if not variant_id:
				product = service.get_product(row.medusa_product_id)
				variant_id = _match_variant_id(product, item)
			if variant_id:
				variant_payload = _variant_update_payload(item, template, settings=settings)
				service.update_variant(row.medusa_product_id, variant_id, variant_payload)
				row.medusa_variant_id = variant_id

		from medusa_connector.utils.sync_guard import mark_product_exported

		mark_product_exported(row.medusa_product_id)
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=row.medusa_product_id,
			variant_id=row.medusa_variant_id if not cint(row.has_variants) else None,
			variant_of=row.variant_of,
			sku=item.name if not cint(row.has_variants) else None,
			has_variants=cint(row.has_variants),
			status="Disabled" if item.disabled else "Active",
		)
		update_sync_log(
			log_name,
			status="Success",
			message=f"Updated {item.name} → {row.medusa_product_id}",
			updated=1,
			response_data=updated,
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


def _build_create_payload(item, template, settings, service: ProductService) -> dict:
	status = "published" if settings.get("sync_new_item_as_published") else "draft"
	if item.disabled or template.disabled:
		status = "draft"

	payload: dict = {
		"title": template.item_name or template.name,
		"status": status,
		"description": _plain_description(template.description),
		"external_id": template.name,
		"metadata": _product_metadata(template, item),
	}

	if item.variant_of:
		payload["options"] = _template_options(template)
		payload["variants"] = [_variant_payload(item, template, settings=settings)]
	else:
		payload["options"] = [{"title": DEFAULT_OPTION_TITLE, "values": [DEFAULT_OPTION_VALUE]}]
		payload["variants"] = [
			{
				"title": DEFAULT_OPTION_VALUE,
				"sku": item.name,
				"options": {DEFAULT_OPTION_TITLE: DEFAULT_OPTION_VALUE},
				"prices": _variant_prices(item, settings=settings),
				"manage_inventory": _manage_inventory(item, settings),
				"metadata": _variant_metadata(item),
				**_optional_barcode(item),
			}
		]

	images = _image_payload(template)
	if images:
		payload["images"] = images
		payload["thumbnail"] = images[0]["url"]

	sales_channel_id = _default_sales_channel_id(settings)
	if sales_channel_id:
		payload["sales_channels"] = [{"id": sales_channel_id}]

	# Weight (Shopify also maps weight when UOM is known).
	if flt(template.weight_per_unit):
		payload["weight"] = flt(template.weight_per_unit)

	return payload


def _build_update_product_payload(item_or_template, settings) -> dict:
	payload: dict = {
		"title": item_or_template.item_name or item_or_template.name,
		"description": _plain_description(item_or_template.description),
		"external_id": item_or_template.name,
		"metadata": _product_metadata(item_or_template, item_or_template),
	}
	if item_or_template.disabled:
		payload["status"] = "draft"
	elif settings.get("sync_new_item_as_published"):
		# Only re-publish when not disabled; do not force published if left draft intentionally.
		pass

	images = _image_payload(item_or_template)
	if images:
		payload["images"] = images
		payload["thumbnail"] = images[0]["url"]

	sales_channel_id = _default_sales_channel_id(settings)
	if sales_channel_id:
		payload["sales_channels"] = [{"id": sales_channel_id}]

	if flt(item_or_template.weight_per_unit):
		payload["weight"] = flt(item_or_template.weight_per_unit)

	return payload


def _write_create_mappings(item, template, created: dict) -> None:
	product_id = created.get("id")
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


def _ensure_product_options(service: ProductService, product_id: str, product: dict, template, item) -> None:
	"""Expand Medusa product options when a new ERPNext attribute value appears."""
	existing = {opt.get("title"): opt for opt in (product.get("options") or []) if opt.get("title")}
	desired = _template_options(template)
	# If options missing entirely, set from template.
	if not existing and desired:
		service.update_product(product_id, {"options": desired})
		return

	# For new values, rebuild options list with union of values (AdminUpdateProductOption).
	changed = False
	merged = []
	for opt in desired:
		title = opt["title"]
		values = list(opt.get("values") or [])
		current = existing.get(title) or {}
		current_values = []
		for v in current.get("values") or []:
			if isinstance(v, dict) and v.get("value"):
				current_values.append(v["value"])
			elif isinstance(v, str):
				current_values.append(v)
		union = list(dict.fromkeys([*current_values, *values]))
		if set(union) != set(current_values) or title not in existing:
			changed = True
		entry: dict = {"title": title, "values": union}
		if current.get("id"):
			entry["id"] = current["id"]
		merged.append(entry)
	if changed and merged:
		service.update_product(product_id, {"options": merged})


def _plain_description(description) -> str:
	if not description:
		return ""
	text = str(description)
	return strip_html(text) if "<" in text else text


def _template_options(template) -> list[dict]:
	"""AdminCreateProductOption: title + values as string[]."""
	options = []
	for row in template.attributes or []:
		attr = frappe.get_doc("Item Attribute", row.attribute)
		values = [v.attribute_value for v in attr.item_attribute_values or [] if v.attribute_value]
		if values:
			options.append({"title": row.attribute, "values": values})
	if not options:
		options = [{"title": DEFAULT_OPTION_TITLE, "values": [DEFAULT_OPTION_VALUE]}]
	return options


def _variant_title(item) -> str:
	"""Short Medusa variant title (avoid pushing polluted ERPNext item_name loops)."""
	attrs = [row.attribute_value for row in (item.attributes or []) if row.attribute_value]
	if attrs:
		return " / ".join(attrs)[:140]
	name = item.item_name or item.name or "Default variant"
	# Strip repeated "Product - Product - ..." residue if present.
	while " - " in name:
		parts = name.split(" - ", 1)
		if parts[0] and parts[1].startswith(parts[0]):
			name = parts[1]
		else:
			break
	return name[:140]


def _variant_payload(item, template, *, settings=None) -> dict:
	"""AdminCreateProductVariant payload."""
	options_map = {
		row.attribute: row.attribute_value
		for row in item.attributes or []
		if row.attribute and row.attribute_value
	}
	if not options_map:
		options_map = {DEFAULT_OPTION_TITLE: DEFAULT_OPTION_VALUE}

	payload = {
		"title": _variant_title(item),
		"sku": item.name,
		"options": options_map,
		"prices": _variant_prices(item, settings=settings),
		"manage_inventory": _manage_inventory(item, settings),
		"metadata": _variant_metadata(item),
	}
	payload.update(_optional_barcode(item))
	if flt(item.weight_per_unit):
		payload["weight"] = flt(item.weight_per_unit)
	return payload


def _variant_update_payload(item, template, *, settings=None) -> dict:
	"""AdminUpdateProductVariant payload (no required fields)."""
	payload = {
		"title": _variant_title(item),
		"sku": item.name,
		"prices": _variant_prices(item, settings=settings),
		"manage_inventory": _manage_inventory(item, settings),
		"metadata": _variant_metadata(item),
	}
	options_map = {
		row.attribute: row.attribute_value
		for row in item.attributes or []
		if row.attribute and row.attribute_value
	}
	if options_map:
		payload["options"] = options_map
	payload.update(_optional_barcode(item))
	if flt(item.weight_per_unit):
		payload["weight"] = flt(item.weight_per_unit)
	return payload


def _variant_prices(item, *, settings=None) -> list[dict]:
	"""Required on create: ``[{currency_code, amount}]`` in major units."""
	currency = _default_currency(settings)
	amount = flt(item.get("standard_rate") or 0)
	# Prefer Item Price from Selling Price List when configured.
	price_list = (settings or frappe.get_cached_doc(SETTING_DOCTYPE)).get("price_list")
	if price_list:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item.name, "price_list": price_list, "selling": 1},
			"price_list_rate",
		)
		if rate is not None:
			amount = flt(rate)
	return [{"currency_code": currency, "amount": amount}]


def _manage_inventory(item, settings=None) -> bool:
	settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
	if not settings.get("update_erpnext_stock_levels_to_medusa"):
		return False
	return bool(item.is_stock_item)


def _optional_barcode(item) -> dict:
	barcodes = getattr(item, "barcodes", None) or []
	if barcodes and barcodes[0].get("barcode"):
		return {"barcode": barcodes[0].barcode}
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
	return {
		"erpnext_item_code": item.name,
		"source": "erpnext",
	}


def _image_payload(item) -> list[dict]:
	"""Admin image objects require ``url``. Only absolute http(s) URLs are sent."""
	url = _absolute_image_url(getattr(item, "image", None))
	if not url:
		return []
	return [{"url": url}]


def _absolute_image_url(image: str | None) -> str | None:
	if not image:
		return None
	image = str(image).strip()
	if image.startswith("http://") or image.startswith("https://"):
		return image
	if image.startswith("/"):
		return get_url(image)
	# ERPNext file path without leading slash
	if image.startswith("files/") or image.startswith("private/files/"):
		return get_url("/" + image)
	return None


def _default_currency(settings=None) -> str:
	settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
	currency = (settings.get("default_currency") or "").lower()
	if currency:
		return currency
	defaults = get_store_defaults(refresh_if_empty=True)
	return (defaults.get("default_currency") or "usd").lower()


def _default_sales_channel_id(settings=None) -> str | None:
	settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
	channel = settings.get("default_sales_channel_id")
	if channel:
		return channel
	defaults = get_store_defaults(refresh_if_empty=False)
	return defaults.get("default_sales_channel_id")


def _match_variant_id(product: dict, item) -> str | None:
	"""Best-effort match of Medusa variant by option values or SKU."""
	wanted = {row.attribute: row.attribute_value for row in item.attributes or []}
	for variant in product.get("variants") or []:
		if variant.get("sku") == item.name:
			return variant.get("id")
		opts = variant.get("options") or []
		vals = {}
		if isinstance(opts, dict):
			vals = opts
		elif isinstance(opts, list):
			for v in opts:
				key = None
				if isinstance(v.get("option"), dict):
					key = v.get("option").get("title")
				elif isinstance(v.get("option"), str):
					key = v.get("option")
				elif v.get("title"):
					key = v.get("title")
				if key and v.get("value"):
					vals[key] = v.get("value")
		if vals and vals == wanted:
			return variant.get("id")
	variants = product.get("variants") or []
	return variants[0].get("id") if variants else None
