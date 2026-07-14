# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Synchronise mapped Medusa products into ERPNext Items + Ecommerce Item.

Uses ERPNext's standard Item Variant APIs (``create_variant`` / ``get_variant``)
and Medusa Admin product fields documented at https://docs.medusajs.com/api/admin.
"""

from __future__ import annotations

import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe.utils import cstr, flt, now_datetime
from frappe.utils.nestedset import get_root_of

from medusa_connector.constants import DEFAULT_ITEM_GROUP, DEFAULT_STOCK_UOM, MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.product.item_mapping import (
	get_ecommerce_items_for_product,
	get_erpnext_item,
	mark_orphaned,
	upsert_mapping,
)


class ProductSync:
	"""Inbound product synchroniser (Medusa → ERPNext)."""

	def __init__(self, settings=None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self.created = 0
		self.updated = 0
		self.skipped = 0

	def sync(self, mapped_product: dict, *, force: bool = False) -> dict:
		"""Create/update the template, attributes, variants, prices, and mappings.

		Returns ``{item_code, action, variant_codes}`` where action is
		``created`` / ``updated`` / ``skipped``.
		"""
		from medusa_connector.utils.sync_guard import inbound_sync, mark_product_imported

		with inbound_sync():
			product_id = mapped_product["medusa_product_id"]
			has_variants = int(mapped_product.get("has_variants") or 0)
			if has_variants:
				existing = get_erpnext_item(product_id, has_variants=1)
			else:
				# Sellable rows are keyed by Variant ID (not Product ID).
				existing = get_erpnext_item(
					variant_id=mapped_product.get("medusa_variant_id"),
					sku=mapped_product.get("sku"),
				) or get_erpnext_item(product_id, has_variants=0)

			self._ensure_stock_uom(mapped_product.get("stock_uom"))
			self._sync_attributes(mapped_product.get("attributes") or [])
			self._ensure_item_group(mapped_product)
			self._ensure_brand(mapped_product.get("brand"))

			if has_variants:
				template_code, action = self._sync_template(mapped_product, existing)
				variant_codes = []
				for variant in mapped_product.get("variants") or []:
					code = self._sync_variant(template_code, product_id, variant, force=force)
					if code:
						variant_codes.append(code)
				self._apply_tags(template_code, mapped_product.get("tags") or [])
				mark_product_imported(product_id, [template_code, *variant_codes])
				return {"item_code": template_code, "action": action, "variant_codes": variant_codes}

			item_code, action = self._sync_simple_item(mapped_product, existing)
			self._apply_tags(item_code, mapped_product.get("tags") or [])
			mark_product_imported(product_id, [item_code])
			return {"item_code": item_code, "action": action, "variant_codes": []}

	def handle_product_delete(self, product_id: str) -> str | None:
		"""Apply Medusa Settings ``on_item_delete`` (Draft=disable / Delete)."""
		strategy = (self.settings.get("on_item_delete") or "Draft").strip()
		if strategy == "Delete":
			return self._delete_product_items(product_id)
		return self.disable_product(product_id)

	def disable_product(self, product_id: str) -> str | None:
		"""Disable ERPNext items linked to a deleted Medusa product."""
		disabled: list[str] = []
		maps = get_ecommerce_items_for_product(product_id)
		# Disable sellables first, then template.
		maps = sorted(maps, key=lambda r: int(r.get("has_variants") or 0))

		for row in maps:
			code = row.erpnext_item_code
			if code and frappe.db.exists("Item", code):
				item = frappe.get_doc("Item", code)
				if not item.disabled:
					item.disabled = 1
					self._save_item(item)
				if code not in disabled:
					disabled.append(code)
			mark_orphaned(product_id, row.variant_id or None)

		return disabled[0] if disabled else None

	def _delete_product_items(self, product_id: str) -> str | None:
		"""Hard-delete mapped Items (best-effort)."""
		maps = get_ecommerce_items_for_product(product_id)
		# Delete variants (has_variants=0) before templates.
		maps = sorted(maps, key=lambda r: int(r.get("has_variants") or 0))
		last = None
		for row in maps:
			if row.erpnext_item_code and frappe.db.exists("Item", row.erpnext_item_code):
				try:
					frappe.delete_doc("Item", row.erpnext_item_code, force=1, ignore_permissions=True)
					last = row.erpnext_item_code
				except Exception:
					# Fallback: disable if linked in transactions
					item = frappe.get_doc("Item", row.erpnext_item_code)
					item.disabled = 1
					self._save_item(item)
					last = item.name
			mark_orphaned(product_id, row.variant_id or None)
		return last

	# ------------------------------------------------------------------
	# Masters
	# ------------------------------------------------------------------
	def _ensure_stock_uom(self, uom: str | None) -> str:
		uom = (uom or DEFAULT_STOCK_UOM).strip() or DEFAULT_STOCK_UOM
		if not frappe.db.exists("UOM", uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
		return uom

	def _ensure_item_group(self, mapped: dict) -> str:
		"""Resolve/create Item Group from categories (hierarchy) or mapped name."""
		categories = mapped.get("categories") or []
		# Prefer deepest category with parent when auto-creating hierarchy.
		if categories:
			for cat in categories:
				name = cat.get("name")
				if not name:
					continue
				parent_name = cat.get("parent_name")
				return self._ensure_group(name, parent_name)
		name = mapped.get("item_group") or self.settings.get("item_group") or DEFAULT_ITEM_GROUP
		return self._ensure_group(name, None)

	def _ensure_group(self, name: str, parent_name: str | None) -> str:
		name = (name or DEFAULT_ITEM_GROUP).strip()
		if frappe.db.exists("Item Group", name):
			return name
		root = get_root_of("Item Group")
		parent = root
		if parent_name:
			parent = self._ensure_group(parent_name, None)
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": parent,
				"is_group": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _ensure_brand(self, brand: str | None) -> str | None:
		if not brand:
			return None
		brand = brand.strip()
		if not brand:
			return None
		if not frappe.db.exists("Brand", brand):
			frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
		return brand

	def _sync_attributes(self, attributes: list[dict]) -> None:
		for attribute in attributes:
			name = attribute.get("name")
			if not name:
				continue
			values = list(dict.fromkeys(v for v in (attribute.get("values") or []) if v))
			if frappe.db.exists("Item Attribute", name):
				item_attribute = frappe.get_doc("Item Attribute", name)
				if item_attribute.numeric_values:
					continue
				existing_values = {row.attribute_value for row in item_attribute.item_attribute_values}
				existing_abbrs = {row.abbr for row in item_attribute.item_attribute_values}
				changed = False
				for value in values:
					if value in existing_values:
						continue
					item_attribute.append(
						"item_attribute_values",
						{
							"attribute_value": value,
							"abbr": self._unique_abbr(value, existing_abbrs),
						},
					)
					existing_values.add(value)
					changed = True
				if changed:
					item_attribute.save(ignore_permissions=True)
			else:
				used: set[str] = set()
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": name,
						"item_attribute_values": [
							{
								"attribute_value": value,
								"abbr": self._unique_abbr(value, used),
							}
							for value in values
						],
					}
				).insert(ignore_permissions=True)

	@staticmethod
	def _unique_abbr(value: str, used: set[str]) -> str:
		base = cstr(value)[:10] or "VAL"
		abbr = base
		idx = 1
		while abbr in used:
			suffix = str(idx)
			abbr = f"{base[: max(1, 10 - len(suffix))]}{suffix}"
			idx += 1
		used.add(abbr)
		return abbr

	# ------------------------------------------------------------------
	# Item create / update
	# ------------------------------------------------------------------
	def _sync_template(self, mapped: dict, existing) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		item_code = existing.name if existing else mapped["item_code"]
		if not existing and frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			action = "updated"
			self.updated += 1
		elif existing:
			item = existing
			action = "updated"
			self.updated += 1
		else:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": mapped["item_name"],
					"is_stock_item": int(mapped.get("is_stock_item", 0)),
					"is_sales_item": 1,
				}
			)
			action = "created"
			self.created += 1

		self._apply_item_fields(item, mapped, has_variants=1)
		self._save_item(item)
		self._sync_item_price(item.name, mapped)

		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			has_variants=1,
			status="Disabled" if item.disabled else "Active",
		)
		return item.name, action

	def _sync_simple_item(self, mapped: dict, existing) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		if not existing and mapped.get("sku") and frappe.db.exists("Item", mapped["sku"]):
			existing = frappe.get_doc("Item", mapped["sku"])
		if not existing and mapped.get("item_code") and frappe.db.exists("Item", mapped["item_code"]):
			existing = frappe.get_doc("Item", mapped["item_code"])

		if existing:
			item = existing
			action = "updated"
			self.updated += 1
		else:
			item_code = mapped.get("sku") or mapped["item_code"]
			if frappe.db.exists("Item", item_code):
				item = frappe.get_doc("Item", item_code)
				action = "updated"
				self.updated += 1
			else:
				item = frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item_code,
						"item_name": mapped["item_name"],
						"is_stock_item": int(mapped.get("is_stock_item", 0)),
						"is_sales_item": 1,
					}
				)
				action = "created"
				self.created += 1

		self._apply_item_fields(item, mapped, has_variants=0)
		self._save_item(item)
		self._sync_item_price(item.name, mapped)

		variant_id = mapped.get("medusa_variant_id")
		if not variant_id:
			frappe.throw(f"Medusa product {product_id} has no default variant id; cannot map sellable item.")
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=mapped.get("sku") or item.name,
			has_variants=0,
			status="Disabled" if item.disabled else "Active",
		)
		return item.name, action

	def _sync_variant(self, template_code: str, product_id: str, variant: dict, *, force: bool) -> str | None:
		variant_id = variant.get("medusa_variant_id")
		attrs = variant.get("attributes") or {}
		if not attrs:
			return None
		if not variant_id:
			return None

		# Lookup by Variant ID / SKU (integration_item_code is the Variant ID).
		mapped_item = get_erpnext_item(variant_id=variant_id, sku=variant.get("sku"))
		if mapped_item:
			self._update_variant_item(mapped_item, variant)
			self._finish_variant_mapping(mapped_item, product_id, variant, template_code)
			self.updated += 1
			return mapped_item.name

		existing_code = get_variant(template_code, attrs)
		if existing_code:
			item = frappe.get_doc("Item", existing_code)
			self._update_variant_item(item, variant)
			self._finish_variant_mapping(item, product_id, variant, template_code)
			self.updated += 1
			return item.name

		variant_doc = create_variant(template_code, attrs)
		if variant.get("sku") and not frappe.db.exists("Item", variant["sku"]):
			variant_doc.item_code = variant["sku"]
		self._apply_variant_fields(variant_doc, variant)
		self._save_item(variant_doc)
		self._sync_item_price(variant_doc.name, variant)
		self._finish_variant_mapping(variant_doc, product_id, variant, template_code)
		self.created += 1
		return variant_doc.name

	def _finish_variant_mapping(self, item, product_id: str, variant: dict, template_code: str) -> None:
		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant.get("medusa_variant_id"),
			sku=variant.get("sku") or item.name,
			variant_of=template_code,
			has_variants=0,
			status="Disabled" if item.disabled else "Active",
		)

	def _update_variant_item(self, item, variant: dict) -> None:
		before = item.as_dict()
		self._apply_variant_fields(item, variant)
		# Only save when something changed (cheap check via a few fields).
		if (
			item.item_name != before.get("item_name")
			or item.image != before.get("image")
			or flt(item.standard_rate) != flt(before.get("standard_rate"))
			or flt(item.weight_per_unit) != flt(before.get("weight_per_unit"))
			or item.get("gst_hsn_code") != before.get("gst_hsn_code")
			or item.disabled != before.get("disabled")
			or int(item.is_stock_item or 0) != int(before.get("is_stock_item") or 0)
		):
			self._save_item(item)
		else:
			# Barcode / price may still need update without main field diffs.
			if variant.get("barcode"):
				self._ensure_barcode(item, variant.get("barcode"))
				if item.has_value_changed("barcodes"):
					self._save_item(item)
		self._sync_item_price(item.name, variant)

	def _apply_variant_fields(self, item, variant: dict) -> None:
		if variant.get("item_name"):
			new_name = (variant.get("item_name") or "")[:140]
			if new_name and item.item_name != new_name:
				item.item_name = new_name
		if variant.get("image"):
			item.image = variant["image"]
		if variant.get("standard_rate") is not None:
			item.standard_rate = flt(variant.get("standard_rate"))
		if variant.get("weight") is not None:
			item.weight_per_unit = flt(variant.get("weight"))
			item.weight_uom = item.weight_uom or self.settings.get("default_stock_uom") or DEFAULT_STOCK_UOM
		# Medusa product status → Item.disabled (publish enables; draft disables).
		if "disabled" in variant:
			item.disabled = int(variant.get("disabled") or 0)
		# Maintain Stock only when Medusa Manage Inventory is enabled for this variant.
		if "is_stock_item" in variant:
			item.is_stock_item = int(variant.get("is_stock_item") or 0)
		elif "manage_inventory" in variant:
			item.is_stock_item = 1 if variant.get("manage_inventory") else 0
		self._apply_country(item, variant.get("origin_country"))
		self._apply_variant_hsn_code(item, variant)
		if variant.get("barcode"):
			self._ensure_barcode(item, variant["barcode"])

	def apply_inventory_item_fields(self, item, inv: dict) -> bool:
		"""Apply Medusa Inventory Item fields onto an ERPNext Item.

		Used by ``inventory-item.*`` webhooks (Admin Inventory module). Inventory
		Item is the source of truth for ``hs_code``, weight, dimensions, material,
		``origin_country``, and ``mid_code`` when those are edited in Medusa
		Inventory (not the Product catalog).

		Returns ``True`` when the Item document was mutated and should be saved.
		"""
		if not inv:
			return False

		before = {
			"weight_per_unit": item.weight_per_unit,
			"gst_hsn_code": item.get("gst_hsn_code"),
			"country_of_origin": item.get("country_of_origin"),
			"description": item.description or "",
			"image": item.image,
			"item_name": item.item_name,
		}

		# Weight (native Item field)
		if inv.get("weight") is not None:
			item.weight_per_unit = flt(inv.get("weight"))
			item.weight_uom = (
				item.weight_uom
				or item.stock_uom
				or self.settings.get("default_stock_uom")
				or DEFAULT_STOCK_UOM
			)

		# HSN / HS code — Inventory Item.hs_code is authoritative for this event.
		# Full Admin GET always includes the key; null/"" means cleared in Inventory.
		# Missing key (partial payload) leaves existing HSN unchanged.
		if "hs_code" in inv:
			hs = inv.get("hs_code")
			if hs not in (None, ""):
				self._apply_hsn_code(item, hs, required_if_india_compliance=False)
			else:
				self._clear_hsn_if_blank_source(item)

		# Country of origin (ISO-2 → ERPNext Country)
		if inv.get("origin_country"):
			self._apply_country(item, inv.get("origin_country"))

		# Thumbnail → Item image when provided
		if inv.get("thumbnail"):
			item.image = inv["thumbnail"]

		# Optional display title (do not rename template/variant codes)
		title = (inv.get("title") or "").strip()
		if title and not item.get("has_variants"):
			new_name = title[:140]
			if new_name and item.item_name != new_name:
				item.item_name = new_name

		# Material, MID, L/W/H live in the non-destructive catalog meta block
		self._update_inventory_catalog_meta(item, inv)

		return (
			flt(item.weight_per_unit) != flt(before["weight_per_unit"])
			or item.get("gst_hsn_code") != before["gst_hsn_code"]
			or item.get("country_of_origin") != before["country_of_origin"]
			or (item.description or "") != before["description"]
			or item.image != before["image"]
			or item.item_name != before["item_name"]
		)

	def _update_inventory_catalog_meta(self, item, inv: dict) -> None:
		"""Merge material / MID / dimensions into the medusa-catalog description block.

		Preserves Handle / Type / Collection markers written by product sync.
		Uses plain-text delimiters (HTML comments are stripped from Item Text fields).
		"""
		desc = item.description or ""
		markers, order, pre, post = self._parse_catalog_meta(desc)

		def _set_marker(key: str, value: str | None) -> None:
			if value:
				markers[key] = f"{key}: {value}"
				if key not in order:
					order.append(key)
			elif key in markers:
				del markers[key]
				if key in order:
					order.remove(key)

		if "material" in inv:
			_set_marker("Material", (inv.get("material") or "").strip() or None)
		if "mid_code" in inv:
			_set_marker("MID", (inv.get("mid_code") or "").strip() or None)

		if any(k in inv for k in ("length", "width", "height")):
			dims = []
			for label, key in (("L", "length"), ("W", "width"), ("H", "height")):
				if inv.get(key) is not None and inv.get(key) != "":
					dims.append(f"{label}={inv[key]}")
			_set_marker("Dimensions", ", ".join(dims) if dims else None)

		if not markers and not any(k in inv for k in ("material", "mid_code", "length", "width", "height")):
			return

		item.description = self._render_catalog_meta(pre, post, markers, order)

	@staticmethod
	def _catalog_meta_delimiters() -> tuple[str, str]:
		# Plain-text markers (HTML comments are stripped from Item Text fields).
		return "[medusa-catalog]", "[/medusa-catalog]"

	@staticmethod
	def _parse_catalog_meta(desc: str) -> tuple[dict[str, str], list[str], str, str]:
		"""Return markers, key order, text before block, text after block."""
		start, end = ProductSync._catalog_meta_delimiters()
		# Legacy HTML-comment delimiters (may still exist on older rows).
		legacy = ("<!-- medusa-catalog -->", "<!-- /medusa-catalog -->")
		markers: dict[str, str] = {}
		order: list[str] = []
		pre, post = desc, ""

		block_body = None
		for s, e in ((start, end), legacy):
			if s in desc and e in desc:
				pre = desc.split(s, 1)[0].rstrip()
				rest = desc.split(s, 1)[1]
				block_body = rest.split(e, 1)[0].strip()
				post = rest.split(e, 1)[1].lstrip()
				break

		if block_body is not None:
			for line in block_body.splitlines():
				line = line.strip()
				if not line or ":" not in line:
					continue
				key = line.split(":", 1)[0].strip()
				markers[key] = line
				if key not in order:
					order.append(key)
		else:
			# Recover orphaned marker lines written when delimiters were stripped.
			orphan_keys = ("Handle", "Type", "Collection", "Material", "MID", "Dimensions")
			kept_lines = []
			for line in desc.splitlines():
				stripped = line.strip()
				matched = False
				for key in orphan_keys:
					if stripped.startswith(f"{key}:"):
						markers[key] = stripped
						if key not in order:
							order.append(key)
						matched = True
						break
				if not matched and stripped:
					kept_lines.append(line)
			pre = "\n".join(kept_lines).strip()
			post = ""

		return markers, order, pre, post

	@staticmethod
	def _render_catalog_meta(pre: str, post: str, markers: dict[str, str], order: list[str]) -> str:
		start, end = ProductSync._catalog_meta_delimiters()
		lines = [markers[k] for k in order if k in markers]
		parts: list[str] = []
		if pre:
			parts.append(pre.rstrip())
		if lines:
			parts.append(f"{start}\n" + "\n".join(lines) + f"\n{end}")
		if post:
			parts.append(post.lstrip())
		return "\n\n".join(parts).strip()

	# ------------------------------------------------------------------
	# Field application
	# ------------------------------------------------------------------
	def _apply_item_fields(self, item, mapped: dict, *, has_variants: int) -> None:
		item.item_name = mapped.get("item_name") or item.item_name
		item.description = mapped.get("description") or item.description
		item.item_group = mapped.get("item_group") or item.item_group or DEFAULT_ITEM_GROUP
		item.stock_uom = self._ensure_stock_uom(mapped.get("stock_uom") or item.stock_uom)
		if mapped.get("image"):
			item.image = mapped["image"]
		# Explicit 0/1 from mapper (published → 0, draft → 1). Do not use `or 0`
		# alone on get — keep int() so Check field always receives 0 or 1.
		if "disabled" in mapped:
			item.disabled = 1 if int(mapped.get("disabled") or 0) else 0
		item.has_variants = int(has_variants)
		item.is_sales_item = 1
		# Maintain Stock mirrors Medusa Manage Inventory (mapper already excludes gift cards).
		item.is_stock_item = int(mapped.get("is_stock_item", 0))

		if mapped.get("brand") and frappe.get_meta("Item").has_field("brand"):
			item.brand = self._ensure_brand(mapped.get("brand"))

		if not has_variants and mapped.get("standard_rate") is not None:
			item.standard_rate = flt(mapped.get("standard_rate"))

		if mapped.get("weight") is not None:
			item.weight_per_unit = flt(mapped.get("weight"))
			item.weight_uom = item.weight_uom or item.stock_uom

		self._apply_country(item, mapped.get("origin_country"))
		# Template ← Medusa product-level HSN only.
		# Simple Item ← variant own or product-level (mapped.gst_hsn_code).
		# Variants ← _apply_variant_hsn_code (own → product → parent → blank).
		hs = mapped.get("gst_hsn_code")
		if has_variants and not hs:
			self._clear_hsn_if_blank_source(item)
		else:
			self._apply_hsn_code(
				item,
				hs,
				required_if_india_compliance=not has_variants,
			)

		if mapped.get("barcode") and not has_variants:
			self._ensure_barcode(item, mapped["barcode"])

		if has_variants:
			item.attributes = []
			for attribute in mapped.get("attributes") or []:
				if attribute.get("name"):
					item.append("attributes", {"attribute": attribute["name"]})

		warehouse = mapped.get("default_warehouse") or self.settings.get("warehouse")
		if warehouse:
			self._ensure_item_default(item, warehouse)

		# Store Medusa handle / type / collection in description footer (no custom fields).
		self._append_catalog_meta(item, mapped)

	def _append_catalog_meta(self, item, mapped: dict) -> None:
		"""Keep non-destructive catalog metadata markers on the description."""
		existing, order, pre, post = self._parse_catalog_meta(item.description or "")
		# Prefer new mapped values; keep prior inventory markers when product omits them.
		markers: dict[str, str] = dict(existing)
		new_order: list[str] = list(order)

		def _set(key: str, value: str | None) -> None:
			if value:
				markers[key] = f"{key}: {value}"
				if key not in new_order:
					new_order.append(key)
			elif key in markers:
				# Product sync overwrites when it has a value; blank product fields
				# leave existing material/MID/dims alone (inventory may own them).
				pass

		_set("Handle", mapped.get("handle"))
		_set("Type", mapped.get("product_type"))
		_set("Collection", mapped.get("collection"))
		if mapped.get("material"):
			_set("Material", mapped.get("material"))
		if mapped.get("mid_code"):
			_set("MID", mapped.get("mid_code"))
		dims = []
		for label, key in (("L", "length"), ("W", "width"), ("H", "height")):
			if mapped.get(key) is not None:
				dims.append(f"{label}={mapped[key]}")
		if dims:
			_set("Dimensions", ", ".join(dims))

		if not markers:
			return
		# Prefer product description body when provided (pre may be old body).
		body = (mapped.get("description") or pre or "").strip()
		# If item.description was already set to product description above, avoid
		# double-prefixing: use current parse pre when body matches.
		item.description = self._render_catalog_meta(body or pre, post, markers, new_order)

	def _ensure_item_default(self, item, warehouse: str) -> None:
		from erpnext import get_default_company

		company = get_default_company()
		if not item.item_defaults:
			item.append(
				"item_defaults",
				{"company": company, "default_warehouse": warehouse},
			)
		else:
			item.item_defaults[0].default_warehouse = warehouse

	@staticmethod
	def _apply_country(item, origin_country: str | None) -> None:
		if not origin_country or not frappe.get_meta("Item").has_field("country_of_origin"):
			return
		code = str(origin_country).strip()
		# Medusa uses ISO-2; ERPNext Country is full name — match by code or name.
		country = frappe.db.get_value("Country", {"code": code}, "name") or (
			code if frappe.db.exists("Country", code) else None
		)
		if country:
			item.country_of_origin = country

	@staticmethod
	def _ensure_barcode(item, barcode: str) -> None:
		barcode = str(barcode).strip()
		if not barcode:
			return
		existing = {row.barcode for row in item.barcodes or []}
		if barcode not in existing:
			item.append("barcodes", {"barcode": barcode})

	def _apply_variant_hsn_code(self, item, variant: dict) -> bool:
		"""Resolve final HSN for an ERPNext Item Variant, then validate/apply.

		1. Medusa ProductVariant.hs_code → this Item Variant only (independent
		   per variant; never overwrites another variant's code).
		2. Else Medusa Product.hs_code (``product_hs_code``) → inherit product-level.
		3. Else ERPNext template ``gst_hsn_code`` if this Item is a variant
		   (covers template already synced with product-level HSN).
		4. Else leave blank.

		India Compliance validates the final resolved code only.
		"""
		variant_hs = (variant.get("gst_hsn_code") or "").strip() or None
		product_hs = (variant.get("product_hs_code") or "").strip() or None
		has_own = bool(variant.get("has_variant_hs_code") or variant_hs)

		if has_own:
			# Variant-specific Medusa HSN — do not replace with product default.
			return self._apply_hsn_code(item, variant_hs, required_if_india_compliance=True)

		if product_hs:
			return self._apply_hsn_code(item, product_hs, required_if_india_compliance=True)

		parent = item.get("variant_of")
		if parent and frappe.db.exists("Item", parent):
			parent_hs = frappe.db.get_value("Item", parent, "gst_hsn_code")
			if parent_hs:
				return self._apply_hsn_code(item, parent_hs, required_if_india_compliance=False)

		return self._clear_hsn_if_blank_source(item)

	@staticmethod
	def _clear_hsn_if_blank_source(item) -> bool:
		"""Leave / clear Item HSN when Medusa sent no code for this level."""
		if not frappe.get_meta("Item").has_field("gst_hsn_code"):
			return False
		if not item.get("gst_hsn_code"):
			return False
		item.gst_hsn_code = ""
		return True

	def _apply_hsn_code(self, item, hs_code: str | None, *, required_if_india_compliance: bool) -> bool:
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

	def _sync_item_price(self, item_code: str, mapped: dict) -> None:
		"""Create/update Item Price from Medusa variant prices (default Price List)."""
		price_list = self.settings.get("price_list")
		if not price_list or not frappe.db.exists("Price List", price_list):
			return
		rate = mapped.get("standard_rate")
		if rate is None and mapped.get("prices"):
			rate = mapped["prices"][0].get("amount")
		if rate is None:
			return
		rate = flt(rate)
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list, "selling": 1},
			"name",
		)
		if existing:
			if flt(frappe.db.get_value("Item Price", existing, "price_list_rate")) != rate:
				frappe.db.set_value("Item Price", existing, "price_list_rate", rate, update_modified=False)
			return
		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate,
				"selling": 1,
			}
		)
		doc.insert(ignore_permissions=True)

	def _apply_tags(self, item_code: str, tags: list[str]) -> None:
		if not tags or not item_code:
			return
		try:
			from frappe.desk.doctype.tag.tag import add_tag

			for tag in tags:
				if tag:
					add_tag(tag, "Item", item_code)
		except Exception:
			# Tag module optional / permission issues should not fail product sync.
			frappe.logger("medusa_connector").warning(f"Could not apply tags {tags} to Item {item_code}")

	@staticmethod
	def _save_item(item) -> None:
		"""Persist Item for inbound Medusa sync.

		Handles concurrent writers (parallel webhooks / inventory field sync) that
		would otherwise raise ``TimestampMismatchError``. Frappe's
		``set_user_and_timestamp`` snapshots ``self.modified`` into
		``_original_modified`` immediately before ``check_if_latest``, so we always
		refresh ``modified`` from the DB right before ``save``. On a remaining race,
		reload the latest row, re-apply our field values, and retry.
		"""
		ProductSync._set_item_save_flags(item)

		is_new = item.is_new() or not item.name or not frappe.db.exists(item.doctype, item.name)
		if is_new:
			item.insert(ignore_permissions=True, ignore_mandatory=True)
			return

		max_attempts = 3
		last_exc: Exception | None = None
		for attempt in range(max_attempts):
			try:
				# set_user_and_timestamp copies self.modified → _original_modified.
				db_modified = frappe.db.get_value(item.doctype, item.name, "modified")
				if db_modified:
					item.modified = db_modified
				item.save(ignore_permissions=True)
				return
			except frappe.TimestampMismatchError as exc:
				last_exc = exc
				if attempt + 1 >= max_attempts:
					break
				# Another job updated the Item between load and save — merge into latest.
				prepared = item.as_dict()
				item = frappe.get_doc(item.doctype, item.name)
				ProductSync._merge_prepared_item_fields(item, prepared)
				ProductSync._set_item_save_flags(item)

		if last_exc:
			raise last_exc

	@staticmethod
	def _set_item_save_flags(item) -> None:
		item.flags.from_medusa = True
		item.flags.from_integration = True
		item.flags.ignore_mandatory = True
		item.flags.ignore_version = True
		item.flags.dont_update_variants = True

	@staticmethod
	def _merge_prepared_item_fields(target, prepared: dict) -> None:
		"""Copy inbound field values onto a freshly loaded Item for save retry."""
		skip = {
			"name",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"docstatus",
			"idx",
			"doctype",
			"parent",
			"parenttype",
			"parentfield",
		}
		for key, value in prepared.items():
			if not key or key.startswith("_") or key in skip:
				continue
			try:
				target.set(key, value)
			except Exception:
				# Skip read-only / virtual fields that cannot be set on the target.
				pass
