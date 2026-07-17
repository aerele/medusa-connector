# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Map Medusa Admin Product payloads to ERPNext-oriented dicts (no side effects).

Field sources follow Medusa Admin API Product / Product Variant models:
https://docs.medusajs.com/api/admin — Products, Product Variants, Options,
Categories, Collections, Tags, Types.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt
from frappe.utils.nestedset import get_root_of

from medusa_connector.constants import SETTING_DOCTYPE


class ProductMapper:
	"""Pure mapping: Medusa product payload → ERPNext-oriented dict."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)

	@staticmethod
	def _validate(product: dict) -> None:
		"""Validate required Medusa product fields."""
		if not product.get("id"):
			raise ValueError("Medusa product payload does not contain an id")

	@staticmethod
	def _metadata(source: dict | None) -> dict[str, object]:
		if not isinstance(source, dict):
			return {}
		metadata = source.get("metadata")
		return metadata if isinstance(metadata, dict) else {}

	def map(self, product: dict) -> dict:
		"""Map a complete Medusa product payload to a template + variants structure."""
		self._validate(product)

		product_id = product.get("id")
		title = product.get("title")
		status = str(product.get("status") or "draft").strip().lower()
		thumbnail = product.get("thumbnail")
		origin_country = product.get("origin_country")

		options = product.get("options") or []
		variants = product.get("variants") or []
		metadata = self._metadata(product)

		has_variants = self._has_variants(product)
		primary_variant = variants[0] if variants else None

		primary_sku = primary_variant.get("sku") if primary_variant else None
		barcode = self._barcode(primary_variant) if (not has_variants and primary_variant) else None

		dims = self._resolve_product_dimensions(product, primary_variant, has_variants)
		product_hs = self._extract_hs_code(product, metadata)
		hs_code = self._resolve_hs_code(primary_variant, product_hs, has_variants)
		images = self._all_image_urls(product)
		thumbnail = thumbnail or (images[0] if images else None)

		disabled = 0 if status == "published" else 1
		manage_inventory = self._resolve_manage_inventory(
			variants,
			primary_variant,
			has_variants,
		)
		is_stock_item = self._is_stock_item(manage_inventory=manage_inventory)

		item_code = self._resolve_item_code(
			title=title,
			product_id=product_id,
			primary_sku=primary_sku,
			has_variants=has_variants,
		)

		return {
			"medusa_product_id": product_id,
			"item_code": item_code,
			"item_name": (title or product_id)[:140],
			"description": self._build_description(product),
			"raw_status": status,
			"disabled": disabled,
			"image": thumbnail,
			"item_group": self._map_item_group(product),
			"stock_uom": self.settings.get("default_stock_uom") or "Nos",
			"default_warehouse": self.settings.get("warehouse"),
			"has_variants": int(has_variants),
			# Template rows never carry a variant's sku — ItemService already
			# ignores this field when has_variants=1, but we don't hand it a
			# value that implies otherwise.
			"sku": None if has_variants else primary_sku,
			"barcode": barcode,
			"standard_rate": self._primary_price(primary_variant) if not has_variants else 0,
			"prices": self._prices(primary_variant) if not has_variants else [],
			"medusa_variant_id": None if has_variants else (primary_variant or {}).get("id"),
			"gst_hsn_code": hs_code,
			"origin_country": origin_country,
			"weight": dims.get("weight"),
			"length": dims.get("length"),
			"width": dims.get("width"),
			"height": dims.get("height"),
			"is_stock_item": is_stock_item,
			"brand": self._brand_name(product, metadata),
			"categories": self._categories(product),
			"tags": self._tags(product),
			"attributes": self._build_product_attributes(options),
			"variants": [
				self._map_variant(
					variant,
					product,
					options,
					product_hs=product_hs,
					disabled=disabled,
				)
				for variant in variants
				if has_variants
			],
		}

	@staticmethod
	def _resolve_item_code(
		*, title: str | None, product_id: str, primary_sku: str | None, has_variants: bool
	) -> str:
		"""Templates: title → product_id (never a variant sku). Simple: sku → title → product_id."""
		if has_variants:
			return title or product_id
		return primary_sku or title or product_id

	@staticmethod
	def _resolve_product_dimensions(product: dict, primary_variant: dict | None, has_variants: bool) -> dict:
		"""Resolve base dimensions, layering variant configurations on single-variant structures."""
		if has_variants or not primary_variant:
			return ProductMapper._dimensions(product)
		return ProductMapper._merge_dimensions(product, primary_variant)

	@staticmethod
	def _resolve_hs_code(
		primary_variant: dict | None, product_hs: str | None, has_variants: bool
	) -> str | None:
		"""Resolve proper HSN fallback rules depending on variant structures."""
		if has_variants:
			return product_hs
		own = ProductMapper._extract_hs_code(primary_variant) if primary_variant else None
		return own or product_hs

	@staticmethod
	def _resolve_manage_inventory(variants: list, primary_variant: dict | None, has_variants: bool) -> bool:
		"""Check whether inventory tracking is active for the target structure."""
		if has_variants:
			return any(ProductMapper._manage_inventory_enabled(v) for v in variants)
		return ProductMapper._manage_inventory_enabled(primary_variant)

	@staticmethod
	def _build_product_attributes(options: list) -> list[dict]:
		"""Format top-level variant configurations options list."""
		return [
			{
				"name": option.get("title"),
				"values": [value.get("value") for value in option.get("values") or [] if value.get("value")],
			}
			for option in options
			if option.get("title")
		]

	def _map_item_group(self, product: dict) -> str:
		"""First Medusa category, else collection, else settings default, else root."""
		categories = product.get("categories") or []
		for category in categories:
			if not isinstance(category, dict):
				continue
			name = category.get("name")
			if name:
				return name
		collection = self._collection_title(product)
		if collection:
			return collection
		# Use configured setting or root
		item_group = self.settings.get("item_group")
		if item_group:
			return item_group
		return get_root_of("Item Group")

	@staticmethod
	def _merge_dimensions(base: dict | None, override: dict | None) -> dict:
		"""Return dimensions from base overridden by non-null values from override."""
		merged = ProductMapper._dimensions(base)
		for key, value in ProductMapper._dimensions(override).items():
			if value is not None:
				merged[key] = value
		return merged

	@staticmethod
	def _categories(product: dict) -> list[dict]:
		rows = []
		for category in product.get("categories") or []:
			if not isinstance(category, dict):
				continue
			parent = category.get("parent_category") or {}
			rows.append(
				{
					"id": category.get("id"),
					"name": category.get("name"),
					"handle": category.get("handle"),
					"parent_name": parent.get("name") if isinstance(parent, dict) else None,
				}
			)
		return rows

	@staticmethod
	def _collection_title(product: dict) -> str | None:
		collection = product.get("collection")
		if isinstance(collection, dict):
			return collection.get("title") or collection.get("handle")
		return None

	@staticmethod
	def _brand_name(product: dict, metadata: dict) -> str | None:
		brand = product.get("brand")
		if isinstance(brand, dict):
			return brand.get("name") or brand.get("title")
		if isinstance(brand, str) and brand.strip():
			return brand.strip()
		if metadata.get("brand"):
			return str(metadata["brand"]).strip()
		return None

	@staticmethod
	def _tags(product: dict) -> list[str]:
		tags = []
		for tag in product.get("tags") or []:
			if isinstance(tag, dict) and tag.get("value"):
				tags.append(str(tag["value"]).strip())
			elif isinstance(tag, str) and tag.strip():
				tags.append(tag.strip())
		return tags

	@staticmethod
	def _build_description(product: dict) -> str:
		parts = []
		if product.get("subtitle"):
			parts.append(str(product["subtitle"]).strip())
		if product.get("description"):
			parts.append(str(product["description"]).strip())
		return "\n\n".join(p for p in parts if p)

	@staticmethod
	def _has_variants(product) -> bool:
		"""Return True when the product has real variant options."""
		for option in product.get("options") or []:
			for value in option.get("values") or []:
				if value.get("value") != "Default option value":
					return True

		return False

	@staticmethod
	def _first_image_url(product: dict) -> str | None:
		images = product.get("images") or []
		return images[0].get("url") if images else None

	@staticmethod
	def _all_image_urls(product: dict) -> list[str]:
		urls = []
		seen = set()
		thumb = product.get("thumbnail")
		if thumb:
			urls.append(thumb)
			seen.add(thumb)
		for image in product.get("images") or []:
			if isinstance(image, dict) and image.get("url") and image["url"] not in seen:
				urls.append(image["url"])
				seen.add(image["url"])
		return urls

	@staticmethod
	def _dimensions(source: dict | None) -> dict[str, float | None]:
		if not source:
			return {}
		return {
			"weight": source.get("weight"),
			"length": source.get("length"),
			"width": source.get("width"),
			"height": source.get("height"),
		}

	@staticmethod
	def _barcode(variant: dict | None) -> str | None:
		if not variant:
			return None
		for key in ("barcode", "ean", "upc"):
			if variant.get(key):
				return str(variant[key]).strip()
		return None

	def _primary_price(self, variant: dict | None) -> float:
		prices = self._prices(variant)
		if not prices:
			return 0.0
		currency = (self.settings.get("default_currency") or "").lower()
		if currency:
			for price in prices:
				if str(price.get("currency_code") or "").lower() == currency:
					return flt(price.get("amount"))
		return flt(prices[0].get("amount"))

	@staticmethod
	def _prices(variant: dict | None) -> list[dict]:
		"""Normalise Medusa variant prices (Admin price list entries)."""
		if not variant:
			return []
		out = []
		for price in variant.get("prices") or []:
			if not isinstance(price, dict):
				continue
			amount = price.get("amount")
			if amount is None:
				continue
			out.append(
				{
					"amount": flt(amount),
					"currency_code": (price.get("currency_code") or "").lower(),
					"price_id": price.get("id"),
				}
			)
		if out:
			return out
		calc = variant.get("calculated_price") or {}
		if calc.get("calculated_amount") is not None:
			return [
				{
					"amount": flt(calc.get("calculated_amount")),
					"currency_code": (calc.get("currency_code") or "").lower(),
					"price_id": None,
				}
			]
		return []

	def _map_variant(
		self,
		variant: dict,
		product: dict,
		options: list[dict],
		*,
		product_hs: str | None = None,
		disabled: int = 0,
	) -> dict:
		attributes = self._extract_variant_attributes(variant, options)
		dims = self._resolve_variant_dimensions(variant, product)
		manage_inventory = self._manage_inventory_enabled(variant)
		variant_hs = self._extract_hs_code(variant)

		return {
			"medusa_variant_id": variant.get("id"),
			"item_name": self._variant_item_name(product, variant),
			"sku": variant.get("sku"),
			"barcode": self._barcode(variant),
			"ean": variant.get("ean"),
			"upc": variant.get("upc"),
			"image": variant.get("thumbnail") or self._first_image_url(product),
			"standard_rate": self._primary_price(variant),
			"prices": self._prices(variant),
			"weight": dims.get("weight"),
			"length": dims.get("length"),
			"width": dims.get("width"),
			"height": dims.get("height"),
			"gst_hsn_code": variant_hs,
			"product_hs_code": product_hs,
			"manage_inventory": manage_inventory,
			"allow_backorder": variant.get("allow_backorder"),
			"disabled": int(disabled),
			"attributes": attributes,
		}

	@staticmethod
	def _extract_variant_attributes(variant: dict, options: list[dict]) -> dict:
		"""Parse variant options into an {option_title: value} map."""
		values_by_option_id: dict[str, str] = {}
		values_by_title: dict[str, str] = {}

		for value in variant.get("options") or []:
			val = value.get("value")
			if not val:
				continue
			option_id = value.get("option_id")
			if option_id:
				values_by_option_id[option_id] = val
			option_obj = value.get("option")
			title = option_obj.get("title") if isinstance(option_obj, dict) else option_obj
			title = title or value.get("title")
			if title:
				values_by_title[title] = val

		attributes = {}
		for option in options:
			title = option.get("title")
			if not title:
				continue
			val = values_by_option_id.get(option.get("id")) or values_by_title.get(title)
			if val:
				attributes[title] = val
		return attributes

	@staticmethod
	def _resolve_variant_dimensions(variant: dict, product: dict) -> dict:
		"""Resolve specific variant dimensions with product-level fallback layers."""
		return ProductMapper._merge_dimensions(product, variant)

	@staticmethod
	def _manage_inventory_enabled(variant: dict | None) -> bool:
		"""True when Medusa ProductVariant.manage_inventory is enabled."""
		if not variant:
			return False
		return bool(variant.get("manage_inventory"))

	@staticmethod
	def _is_stock_item(*, manage_inventory: bool) -> int:
		"""ERPNext Maintain Stock only when Medusa manages inventory."""
		return 1 if manage_inventory else 0

	@staticmethod
	def _extract_hs_code(*sources: dict | None) -> str | None:
		"""Return first non-empty ``hs_code`` from the given Medusa dicts only."""
		for source in sources:
			if not isinstance(source, dict):
				continue
			value = source.get("hs_code")
			if value not in (None, ""):
				return str(value).strip()
			meta = source.get("metadata")
			if isinstance(meta, dict) and meta.get("hs_code") not in (None, ""):
				return str(meta["hs_code"]).strip()
		return None

	@staticmethod
	def _variant_item_name(product: dict, variant: dict) -> str:
		"""Build a stable variant Item name without re-prefixing on every sync."""
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
			while out.startswith(prefix):
				out = out[len(prefix) :].strip()
			marker = product_title + " - "
			while marker in out:
				parts = out.split(marker)
				out = marker.join(p for p in parts if p and p != product_title).strip()
				if out.startswith(marker):
					continue
				break
		out = out.lstrip(" -")
		return out or title
