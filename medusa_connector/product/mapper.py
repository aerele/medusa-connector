# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Map Medusa Admin Product payloads to ERPNext-oriented dicts (no side effects).

Field sources follow Medusa Admin API Product / Product Variant models:
https://docs.medusajs.com/api/admin — Products, Product Variants, Options,
Categories, Collections, Tags, Types.
"""

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
		metadata = product.get("metadata") if isinstance(product.get("metadata"), dict) else {}

		description = self._build_description(product)
		item_group = self._map_item_group(product)
		stock_uom = self.settings.get("default_stock_uom") or DEFAULT_STOCK_UOM
		warehouse = self.settings.get("warehouse")
		has_variants = self._has_variants(options, variants)

		primary_sku = None
		primary_variant = variants[0] if variants else None
		if not has_variants and primary_variant:
			primary_sku = primary_variant.get("sku")

		preferred_code = metadata.get("erpnext_item_code") if metadata else None

		inventory_item_id = None
		if not has_variants and primary_variant:
			inventory_item_id = self._inventory_item_id(primary_variant)

		# Product-level dimensions / customs (Admin Product model).
		dims = self._dimensions(product)
		if not has_variants and primary_variant:
			# Prefer variant dimensions when present (documented on ProductVariant).
			v_dims = self._dimensions(primary_variant)
			for key, value in v_dims.items():
				if value is not None:
					dims[key] = value

		# Admin API: Product.hs_code (product-level) and ProductVariant.hs_code
		# (variant-level) are independent fields.
		# - Template Item ← product-level only
		# - Item Variant ← own hs_code, else product-level, else blank
		product_hs = self._extract_hs_code(product, metadata)
		if has_variants:
			hs_code = product_hs
		else:
			# Single sellable SKU: variant-level first, else product-level.
			own = self._extract_hs_code(primary_variant) if primary_variant else None
			hs_code = own or product_hs
		barcode = None
		if not has_variants and primary_variant:
			barcode = self._barcode(primary_variant)

		product_type = self._product_type(product)
		collection = self._collection_title(product)
		brand = self._brand_name(product, metadata)
		tags = self._tags(product)
		categories = self._categories(product)
		images = self._all_image_urls(product)
		thumbnail = product.get("thumbnail") or (images[0] if images else None)

		is_giftcard = bool(product.get("is_giftcard"))
		status = str(product.get("status") or "draft").strip().lower()
		# published → active Item; draft/proposed/rejected → disabled
		disabled = 0 if status == "published" else 1

		# Maintain Stock (is_stock_item) only when Medusa Manage Inventory is on.
		if has_variants:
			# Template: enable stock if any sellable variant manages inventory;
			# each variant Item still gets its own flag in ``_map_variant``.
			manage_inventory = any(self._manage_inventory_enabled(v) for v in variants)
		else:
			manage_inventory = self._manage_inventory_enabled(primary_variant)
		is_stock_item = self._is_stock_item(manage_inventory=manage_inventory, is_giftcard=is_giftcard)

		return {
			"medusa_product_id": product_id,
			"item_code": preferred_code or primary_sku or product_id,
			"item_name": (product.get("title") or product_id)[:140],
			"subtitle": product.get("subtitle") or "",
			"description": description,
			"handle": product.get("handle"),
			"raw_status": status,
			"disabled": disabled,
			"image": thumbnail,
			"images": images,
			"item_group": item_group,
			"stock_uom": stock_uom,
			"default_warehouse": warehouse,
			"has_variants": int(has_variants),
			"sku": primary_sku,
			"barcode": barcode,
			"standard_rate": self._primary_price(primary_variant) if not has_variants else 0,
			"prices": self._prices(primary_variant) if not has_variants else [],
			"medusa_variant_id": None if has_variants else (primary_variant or {}).get("id"),
			"medusa_inventory_item_id": inventory_item_id,
			# Product-level HSN for the ERPNext template (or simple Item).
			"gst_hsn_code": hs_code,
			"product_hs_code": product_hs,
			"mid_code": (primary_variant or product).get("mid_code")
			if not has_variants
			else product.get("mid_code"),
			"origin_country": (primary_variant or product).get("origin_country")
			if not has_variants
			else product.get("origin_country"),
			"material": product.get("material")
			or ((primary_variant or {}).get("material") if not has_variants else None),
			"weight": dims.get("weight"),
			"length": dims.get("length"),
			"width": dims.get("width"),
			"height": dims.get("height"),
			"is_giftcard": is_giftcard,
			"manage_inventory": manage_inventory,
			"is_stock_item": is_stock_item,
			"discountable": product.get("discountable"),
			"external_id": product.get("external_id"),
			"product_type": product_type,
			"collection": collection,
			"brand": brand,
			"categories": categories,
			"tags": tags,
			"metadata": metadata,
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
			"updated_at": product.get("updated_at"),
		}

	# ------------------------------------------------------------------
	# Mapping helpers
	# ------------------------------------------------------------------
	def _map_item_group(self, product: dict) -> str:
		"""Prefer first Medusa category name; else collection; else settings default.

		Category hierarchy is applied later in ProductSync when auto-creating
		Item Groups (parent category names from category parent_category if present).
		"""
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
		return self.settings.get("item_group") or DEFAULT_ITEM_GROUP

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
	def _product_type(product: dict) -> str | None:
		ptype = product.get("type")
		if isinstance(ptype, dict):
			return ptype.get("value")
		return None

	@staticmethod
	def _brand_name(product: dict, metadata: dict) -> str | None:
		# Brand is not a core Product field; common patterns: metadata.brand or linked brand.
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
	def _dimensions(source: dict | None) -> dict:
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

	@staticmethod
	def _inventory_item_id(variant: dict) -> str | None:
		for link in variant.get("inventory_items") or []:
			iid = link.get("inventory_item_id") or (link.get("inventory_item") or {}).get("id")
			if iid:
				return iid
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
		# Fallback: calculated_price (storefront-oriented responses)
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

		dims = self._dimensions(variant)
		# Fall back to product-level dimensions.
		p_dims = self._dimensions(product)
		for key, value in p_dims.items():
			if dims.get(key) is None and value is not None:
				dims[key] = value

		# Maintain Stock only when this Medusa variant has Manage Inventory on.
		manage_inventory = self._manage_inventory_enabled(variant)
		is_stock_item = self._is_stock_item(
			manage_inventory=manage_inventory,
			is_giftcard=bool(product.get("is_giftcard")),
		)

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
			# Own Admin ProductVariant.hs_code only (may differ per variant).
			"gst_hsn_code": self._extract_hs_code(variant),
			"has_variant_hs_code": bool(self._extract_hs_code(variant)),
			# Product-level HSN for inheritance when variant has none.
			"product_hs_code": product_hs,
			"mid_code": variant.get("mid_code") or product.get("mid_code"),
			"origin_country": variant.get("origin_country") or product.get("origin_country"),
			"material": variant.get("material") or product.get("material"),
			"manage_inventory": manage_inventory,
			"is_stock_item": is_stock_item,
			"allow_backorder": variant.get("allow_backorder"),
			# Follow parent Medusa product status (draft → disabled, published → enabled).
			"disabled": int(disabled),
			"metadata": variant.get("metadata") if isinstance(variant.get("metadata"), dict) else {},
			"medusa_inventory_item_id": self._inventory_item_id(variant),
			"attributes": attributes,
		}

	@staticmethod
	def _manage_inventory_enabled(variant: dict | None) -> bool:
		"""True when Medusa ProductVariant.manage_inventory is enabled."""
		if not variant:
			return False
		return bool(variant.get("manage_inventory"))

	@staticmethod
	def _is_stock_item(*, manage_inventory: bool, is_giftcard: bool = False) -> int:
		"""ERPNext Maintain Stock only when Medusa manages inventory (never for gift cards)."""
		if is_giftcard:
			return 0
		return 1 if manage_inventory else 0

	@staticmethod
	def _extract_hs_code(*sources: dict | None) -> str | None:
		"""Return first non-empty ``hs_code`` from the given Medusa dicts only.

		Admin API: Product and ProductVariant each have their own ``hs_code``
		field. Callers choose the source list (variant-only vs product-only).
		"""
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
