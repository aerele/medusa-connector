# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Map Medusa products to ERPNext Item field dicts (no side effects)."""

from __future__ import annotations

import frappe
from frappe.utils import flt

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

		item_group = self._map_item_group(product)
		stock_uom = self.settings.get("default_stock_uom") or DEFAULT_STOCK_UOM
		warehouse = self.settings.get("warehouse")

		has_variants = self._has_variants(options, variants)
		# Prefer a stable human code when a single-variant product carries a SKU.
		primary_sku = None
		primary_variant = variants[0] if variants else None
		if not has_variants and primary_variant:
			primary_sku = primary_variant.get("sku")

		metadata = product.get("metadata") or {}
		# Prefer ERPNext item code stored in metadata when re-importing exports.
		preferred_code = None
		if isinstance(metadata, dict):
			preferred_code = metadata.get("erpnext_item_code")

		inventory_item_id = None
		if not has_variants and primary_variant:
			for link in primary_variant.get("inventory_items") or []:
				inventory_item_id = link.get("inventory_item_id") or (link.get("inventory_item") or {}).get(
					"id"
				)
				if inventory_item_id:
					break

		return {
			"medusa_product_id": product_id,
			"item_code": preferred_code or primary_sku or product_id,
			"item_name": product.get("title") or product_id,
			"description": description,
			"image": product.get("thumbnail") or self._first_image_url(product),
			"images": self._all_image_urls(product),
			"item_group": item_group,
			"stock_uom": stock_uom,
			"default_warehouse": warehouse,
			"disabled": 0 if product.get("status") == "published" else 1,
			"has_variants": int(has_variants),
			"sku": primary_sku,
			"standard_rate": self._primary_price(primary_variant) if not has_variants else 0,
			"medusa_variant_id": None if has_variants else (primary_variant or {}).get("id"),
			"medusa_inventory_item_id": inventory_item_id,
			"handle": product.get("handle"),
			"collection": (product.get("collection") or {}).get("title")
			if isinstance(product.get("collection"), dict)
			else product.get("collection_id"),
			"categories": [
				(c.get("name") or c.get("id"))
				for c in (product.get("categories") or [])
				if isinstance(c, dict)
			],
			"tags": [
				(t.get("value") or t.get("id")) for t in (product.get("tags") or []) if isinstance(t, dict)
			],
			"metadata": metadata if isinstance(metadata, dict) else {},
			"external_id": product.get("external_id"),
			"weight": product.get("weight"),
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

	def _map_item_group(self, product: dict) -> str:
		"""Prefer first Medusa category name when present; else settings default."""
		for category in product.get("categories") or []:
			if isinstance(category, dict) and category.get("name"):
				name = category["name"]
				if frappe.db.exists("Item Group", name):
					return name
				# Do not auto-create nested groups here; fall through to default.
		return self.settings.get("item_group") or DEFAULT_ITEM_GROUP

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

	@staticmethod
	def _all_image_urls(product: dict) -> list[str]:
		urls = []
		for image in product.get("images") or []:
			if isinstance(image, dict) and image.get("url"):
				urls.append(image["url"])
		return urls

	@staticmethod
	def _primary_price(variant: dict | None) -> float:
		if not variant:
			return 0.0
		prices = variant.get("prices") or []
		if not prices:
			# Some list responses only expose calculated_price
			calc = variant.get("calculated_price") or {}
			if calc.get("calculated_amount") is not None:
				return flt(calc.get("calculated_amount"))
			return 0.0
		# Prefer default currency when present on settings.
		currency = (frappe.db.get_single_value(SETTING_DOCTYPE, "default_currency") or "").lower()
		if currency:
			for price in prices:
				if str(price.get("currency_code") or "").lower() == currency:
					return flt(price.get("amount"))
		return flt(prices[0].get("amount"))

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

		inventory_item_id = None
		for link in variant.get("inventory_items") or []:
			inventory_item_id = link.get("inventory_item_id") or (link.get("inventory_item") or {}).get("id")
			if inventory_item_id:
				break

		return {
			"medusa_variant_id": variant.get("id"),
			"item_name": self._variant_item_name(product, variant),
			"sku": variant.get("sku"),
			"barcode": variant.get("barcode") or variant.get("ean") or variant.get("upc"),
			"image": variant.get("thumbnail") or self._first_image_url(product),
			"standard_rate": self._primary_price(variant),
			"weight": variant.get("weight"),
			"metadata": variant.get("metadata") if isinstance(variant.get("metadata"), dict) else {},
			"medusa_inventory_item_id": inventory_item_id,
			"attributes": attributes,
		}

	@staticmethod
	def _variant_item_name(product: dict, variant: dict) -> str:
		"""Build a stable variant Item name without re-prefixing on every sync.

		Previously always did ``product.title - variant.title``. After ERPNext
		exported ``item_name`` as the Medusa variant title, each loop doubled
		the prefix until it exceeded Item.item_name length (140).
		"""
		product_title = (product.get("title") or product.get("id") or "").strip()
		variant_title = (variant.get("title") or variant.get("id") or "").strip()
		variant_title = ProductMapper._strip_repeated_title_prefix(variant_title, product_title)

		if not variant_title or variant_title.lower() in {"default variant", "default"}:
			name = product_title
		elif product_title and variant_title == product_title:
			name = product_title
		elif product_title and variant_title:
			name = f"{product_title} - {variant_title}"
		else:
			name = variant_title or product_title
		return (name or variant.get("id") or "Item")[:140]

	@staticmethod
	def _strip_repeated_title_prefix(title: str, product_title: str) -> str:
		"""Collapse ``Product - Product - L / Black`` → ``L / Black``."""
		if not title:
			return title
		out = title.strip()
		if product_title:
			prefix = product_title + " - "
			# Peel repeated product-title prefixes from either side of the chain.
			while out.startswith(prefix):
				out = out[len(prefix) :].strip()
			# Also handle "Product - Product - Product - X" where spaces differ.
			marker = product_title + " - "
			while marker in out:
				# Keep the suffix after the last product-title occurrence when duplicated.
				parts = out.split(marker)
				# e.g. ["", "", "L / Black"] or ["Medusa T-Shirt", "L / Black"] after partial peel
				out = marker.join(p for p in parts if p and p != product_title).strip()
				if out.startswith(marker):
					continue
				break
		# Final cleanup of leading separators
		out = out.lstrip(" -")
		return out or title
