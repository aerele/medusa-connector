# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item.ecommerce_item import get_erpnext_item
from frappe.utils import flt

from medusa_connector.constants import DEFAULT_ITEM_GROUP, DEFAULT_STOCK_UOM, MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.product.export_products import upsert_mapping
from medusa_connector.product.services.hsn import HSNService
from medusa_connector.product.services.master import MasterService
from medusa_connector.product.services.metadata import MetadataService
from medusa_connector.product.services.persistence import PersistenceService
from medusa_connector.product.services.price import PriceService


class ItemService:
	def __init__(self, settings=None, master: MasterService | None = None) -> None:
		self.settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
		self.master = master or MasterService(self.settings)
		self.hsn = HSNService(self.settings)
		self.metadata = MetadataService(self.settings)
		self.price = PriceService(self.settings)
		self.persistence = PersistenceService(self.settings)

	def sync_template(self, mapped: dict, existing=None) -> tuple[str, str]:
		return self._sync_item(mapped, existing, has_variants=1)

	def sync_simple_item(self, mapped: dict, existing=None) -> tuple[str, str]:
		variant_id = mapped.get("medusa_variant_id")
		if not variant_id:
			frappe.throw(f"Medusa product {mapped['medusa_product_id']} has no variant id")
		return self._sync_item(mapped, existing, has_variants=0, variant_id=variant_id)

	def sync_product_fields_only(self, mapped: dict, existing=None) -> tuple[str, str]:
		"""Sync only product-level fields (for Case 2 Product webhooks).

		For single Item products (no variants), this updates only:
		- Item name, description, item group, brand
		- Images (thumbnail)
		- Tags
		- Status (disabled)
		"""
		variant_id = mapped.get("medusa_variant_id")
		if not variant_id:
			frappe.throw(f"Medusa product {mapped['medusa_product_id']} has no variant id")

		product_id = mapped["medusa_product_id"]
		sku = mapped.get("sku")
		item_code = sku or mapped.get("item_code")

		if existing:
			item_name = existing if isinstance(existing, str) else existing.name
			item = frappe.get_doc("Item", item_name)
			action = "updated"
		elif frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			action = "updated"
		else:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": mapped.get("item_name"),
					"is_sales_item": 1,
					"has_variants": 0,
				}
			)
			action = "created"

		self._apply_product_only_fields(item, mapped)
		self.persistence.save_item(item)

		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=sku,
			has_variants=0,
		)

		return item.name, action

	def sync_variant_fields_only(self, mapped_variant: dict, existing=None) -> tuple[str, str]:
		"""Sync only variant-level fields (for Case 2 Variant webhooks).

		For single Item products (no variants), this updates only:
		- SKU, barcode, EAN, UPC
		- Price
		- Weight and dimensions
		- Manage inventory, allow backorder
		- HSN code, country of origin
		"""
		variant_id = mapped_variant.get("medusa_variant_id")
		if not variant_id:
			frappe.throw("Variant data missing medusa_variant_id")

		sku = mapped_variant.get("sku")

		if existing:
			item_name = existing if isinstance(existing, str) else existing.name
			item = frappe.get_doc("Item", item_name)
			action = "updated"
		else:
			item = frappe.get_doc("Item", sku)
			action = "updated"

		self._apply_variant_only_fields(item, mapped_variant)
		self.persistence.save_item(item)
		self.price.sync_item_price(item.name, mapped_variant)

		return item.name, action

	def _sync_item(
		self, mapped: dict, existing, *, has_variants: int, variant_id: str | None = None
	) -> tuple[str, str]:
		product_id = mapped["medusa_product_id"]
		sku = mapped.get("sku") if not has_variants else None
		item_code = (sku or mapped["item_code"]) if not has_variants else mapped["item_code"]

		if existing:
			item_name = existing if isinstance(existing, str) else existing.name
			item = frappe.get_doc("Item", item_name)
			action = "updated"
		elif frappe.db.exists("Item", item_code):
			item = frappe.get_doc("Item", item_code)
			action = "updated"
		else:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": mapped.get("item_name"),
					"is_sales_item": 1,
					"is_stock_item": int(mapped.get("is_stock_item", 0)),
					"has_variants": has_variants,
				}
			)
			action = "created"

		self.apply_item_fields(item, mapped, has_variants=has_variants)
		self.persistence.save_item(item)

		if not has_variants:
			self.price.sync_item_price(item.name, mapped)

		upsert_mapping(
			erpnext_item_code=item.name,
			medusa_product_id=product_id,
			variant_id=variant_id,
			sku=sku,
			has_variants=has_variants,
		)

		return item.name, action

	def apply_item_fields(self, item, mapped: dict, *, has_variants: int) -> None:
		self._apply_basic_fields(item, mapped, has_variants)
		self._apply_brand_and_pricing(item, mapped, has_variants)
		self._apply_inventory_fields(item, mapped, has_variants=has_variants)
		self._apply_tax_fields(item, mapped, has_variants)
		self._apply_template_fields(item, mapped, has_variants)
		self._apply_defaults(item, mapped)
		self.metadata.apply_product_fields(item, mapped)

	def _apply_product_only_fields(self, item, mapped: dict) -> None:
		"""Apply only product-level fields (for Case 2 Product webhooks)."""
		if mapped.get("item_name"):
			item.item_name = mapped["item_name"]

		if mapped.get("description"):
			item.description = mapped["description"]

		if mapped.get("item_group"):
			item.item_group = mapped["item_group"] or item.item_group or DEFAULT_ITEM_GROUP

		if mapped.get("brand") and frappe.get_meta("Item").has_field("brand"):
			item.brand = self.master.ensure_brand(mapped["brand"])

		if mapped.get("image"):
			item.image = mapped["image"]

		if "disabled" in mapped:
			item.disabled = int(mapped.get("disabled") or 0)

	def _apply_variant_only_fields(self, item, mapped_variant: dict) -> None:
		"""Apply only variant-level fields (for Case 2 Variant webhooks)."""
		if mapped_variant.get("sku"):
			item.item_code = mapped_variant["sku"]

		if mapped_variant.get("weight") is not None:
			item.weight_per_unit = flt(mapped_variant["weight"])
			item.weight_uom = item.weight_uom or self.settings.get("default_stock_uom") or DEFAULT_STOCK_UOM

		if "allow_backorder" in mapped_variant:
			item.allow_negative_stock = int(bool(mapped_variant.get("allow_backorder")))

		if "manage_inventory" in mapped_variant:
			item.is_stock_item = int(bool(mapped_variant.get("manage_inventory")))

		if mapped_variant.get("image"):
			item.image = mapped_variant["image"]

		if "disabled" in mapped_variant:
			item.disabled = int(mapped_variant.get("disabled") or 0)

		# Barcodes
		self.hsn.ensure_barcodes(
			item,
			barcode=mapped_variant.get("barcode"),
			ean=mapped_variant.get("ean"),
			upc=mapped_variant.get("upc"),
		)

		# HSN code and country
		self.hsn.apply_country(item, mapped_variant.get("origin_country"))
		self.hsn.apply_hsn_code(item, mapped_variant.get("gst_hsn_code"))

		# Dimensions
		item_meta = frappe.get_meta("Item")
		for fieldname in ("length", "width", "height"):
			value = mapped_variant.get(fieldname)
			custom_fieldname = f"medusa_custom_{fieldname}"
			if not item_meta.has_field(custom_fieldname):
				continue
			if value is not None and item.get(custom_fieldname) != value:
				item.set(custom_fieldname, value)

	def _apply_basic_fields(self, item, mapped: dict, has_variants: int) -> None:
		item.item_name = mapped.get("item_name") or item.item_name
		item.description = mapped.get("description") or item.description
		item.item_group = mapped.get("item_group") or item.item_group or DEFAULT_ITEM_GROUP
		item.stock_uom = self.master.ensure_stock_uom(mapped.get("stock_uom") or item.stock_uom)

		if mapped.get("image"):
			item.image = mapped["image"]

		if "disabled" in mapped:
			item.disabled = int(mapped.get("disabled") or 0)

		item.has_variants = has_variants
		item.is_sales_item = 1
		item.is_stock_item = int(mapped.get("is_stock_item", item.is_stock_item))

	def _apply_brand_and_pricing(self, item, mapped: dict, has_variants: int) -> None:
		if mapped.get("brand") and frappe.get_meta("Item").has_field("brand"):
			item.brand = self.master.ensure_brand(mapped["brand"])

		if not has_variants and mapped.get("standard_rate") is not None:
			item.standard_rate = flt(mapped["standard_rate"])

	def _apply_inventory_fields(self, item, mapped: dict, *, has_variants: int = 0) -> None:
		if "allow_negative_stock" in mapped:
			item.allow_negative_stock = int(mapped.get("allow_negative_stock") or 0)

		if mapped.get("weight") is not None:
			item.weight_per_unit = flt(mapped["weight"])
			item.weight_uom = item.weight_uom or item.stock_uom

		if not has_variants and (mapped.get("barcode") or mapped.get("ean") or mapped.get("upc")):
			self.hsn.ensure_barcodes(
				item,
				barcode=mapped.get("barcode"),
				ean=mapped.get("ean"),
				upc=mapped.get("upc"),
			)

	def _apply_tax_fields(self, item, mapped: dict, has_variants: int) -> None:
		self.hsn.apply_country(item, mapped.get("origin_country"))
		self.hsn.apply_hsn_code(
			item,
			mapped.get("gst_hsn_code"),
			required_if_india_compliance=not has_variants,
		)

	def _apply_template_fields(self, item, mapped: dict, has_variants: int) -> None:
		if not has_variants:
			return

		item.attributes = []

		for attribute in mapped.get("attributes") or []:
			name = attribute.get("name")
			if name:
				item.append("attributes", {"attribute": name})

	def _apply_defaults(self, item, mapped: dict) -> None:
		warehouse = mapped.get("default_warehouse") or self.settings.get("warehouse")

		if warehouse:
			self.ensure_item_default(item, warehouse)

	def ensure_item_default(self, item, warehouse: str) -> None:
		from erpnext import get_default_company

		company = get_default_company()

		if not item.item_defaults:
			item.append("item_defaults", {"company": company, "default_warehouse": warehouse})
		else:
			item.item_defaults[0].default_warehouse = warehouse
