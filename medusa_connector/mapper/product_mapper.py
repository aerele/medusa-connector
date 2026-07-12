# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Map Medusa products to ERPNext Item field dicts (no side effects)."""

from __future__ import annotations

import frappe

from medusa_connector.constants import DEFAULT_ITEM_GROUP, DEFAULT_STOCK_UOM, SETTING_DOCTYPE


class ProductMapper:
	"""Pure mapping: Medusa product payload → ERPNext-oriented dict."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	def map(self, product: dict) -> dict:
		"""Map a complete Medusa product payload to a template + variants structure."""
		product_id = product.get("id")
		if not product_id:
			raise ValueError("Medusa product payload does not contain an id")

		options = product.get("options") or []
		variants = product.get("variants") or []
		description = product.get("description") or ""
		if product.get("subtitle"):
			description = f"{product['subtitle']}\n\n{description}".strip()

		item_group = self.settings.get("item_group") or DEFAULT_ITEM_GROUP
		stock_uom = self.settings.get("default_stock_uom") or DEFAULT_STOCK_UOM
		warehouse = self.settings.get("warehouse")

		has_variants = self._has_variants(options, variants)
		# Prefer a stable human code when a single-variant product carries a SKU.
		primary_sku = None
		if not has_variants and variants:
			primary_sku = variants[0].get("sku")

		return {
			"medusa_product_id": product_id,
			"item_code": primary_sku or product_id,
			"item_name": product.get("title") or product_id,
			"description": description,
			"image": product.get("thumbnail") or self._first_image_url(product),
			"item_group": item_group,
			"stock_uom": stock_uom,
			"default_warehouse": warehouse,
			"disabled": 0 if product.get("status") == "published" else 1,
			"has_variants": int(has_variants),
			"sku": primary_sku,
			"attributes": [
				{
					"name": option.get("title"),
					"values": [
						value.get("value") for value in option.get("values") or [] if value.get("value")
					],
				}
				for option in options
				if option.get("title")
			],
			"variants": [
				self._map_variant(variant, product, options) for variant in variants if has_variants
			],
			"raw_status": product.get("status"),
			"updated_at": product.get("updated_at"),
		}

	@staticmethod
	def _has_variants(options: list, variants: list) -> bool:
		"""Detect ERPNext template products (multiple sellable variants)."""
		if not options or not variants:
			return False
		if len(variants) > 1:
			return True
		for option in options:
			values = [v.get("value") for v in option.get("values") or [] if v.get("value")]
			if len(values) > 1:
				return True
		return False

	@staticmethod
	def _first_image_url(product: dict) -> str | None:
		images = product.get("images") or []
		return images[0].get("url") if images else None

	def _map_variant(self, variant: dict, product: dict, options: list[dict]) -> dict:
		values_by_option_id = {
			value.get("option_id"): value.get("value")
			for value in variant.get("options") or []
			if value.get("option_id") and value.get("value")
		}
		# Medusa v2 nests the option object: options[].option.title
		values_by_title: dict[str, str] = {}
		for value in variant.get("options") or []:
			if not value.get("value"):
				continue
			option_obj = value.get("option")
			title = None
			if isinstance(option_obj, dict):
				title = option_obj.get("title")
			elif isinstance(option_obj, str):
				title = option_obj
			title = title or value.get("title")
			if title:
				values_by_title[title] = value.get("value")

		attributes = {}
		for option in options:
			title = option.get("title")
			if not title:
				continue
			val = values_by_option_id.get(option.get("id")) or values_by_title.get(title)
			if val:
				attributes[title] = val

		return {
			"medusa_variant_id": variant.get("id"),
			"item_name": f"{product.get('title') or product.get('id')} - {variant.get('title') or variant.get('id')}",
			"sku": variant.get("sku"),
			"barcode": variant.get("barcode") or variant.get("ean") or variant.get("upc"),
			"image": variant.get("thumbnail") or self._first_image_url(product),
			"attributes": attributes,
		}
