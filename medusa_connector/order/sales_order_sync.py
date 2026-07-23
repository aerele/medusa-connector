# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Sales Order creation from a Medusa order (items, taxes, shipping, currency)."""

from __future__ import annotations

import json
from typing import Any

import frappe
from ecommerce_core.utils.price_list import get_dummy_price_list
from ecommerce_core.utils.taxation import get_dummy_tax_category
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
)
from medusa_connector.order._shared import resolve_item_code, status_label


class SalesOrderSync:
	def __init__(self, settings):
		self.settings = settings

	# ------------------------------------------------------------------ #
	# public API
	# ------------------------------------------------------------------ #
	@staticmethod
	def get_sales_order_doc(order_id: str):
		name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(order_id)}, "name")
		return frappe.get_doc("Sales Order", name) if name else None

	def ensure_items(self, order: dict) -> None:
		"""Sync any Medusa products not yet mapped to an Item before creating the Sales Order."""
		from medusa_connector.medusa.product import ProductService
		from medusa_connector.product.mapper import ProductMapper
		from medusa_connector.product.sync import ProductSync

		unmapped_ids = {
			cstr(line.get("product_id"))
			for line in order.get("items") or []
			if isinstance(line, dict) and line.get("product_id") and not resolve_item_code(line)
		}
		if not unmapped_ids:
			return

		product_service = ProductService()
		product_mapper = ProductMapper()
		product_sync = ProductSync()
		for medusa_product_id in unmapped_ids:
			product = product_service.get_product(medusa_product_id)
			if product:
				product_sync.sync(product_mapper.map(product), force=True)

	def create(self, order: dict, *, customer: str, addresses: dict, company: str | None = None):
		"""Build and submit the Sales Order. Caller (OrderSync) logs failures."""
		order_id = cstr(order.get("id"))
		delivery_date = getdate(order.get("created_at")) or nowdate()

		items = self._get_order_items(order.get("items") or [], delivery_date)
		if not items:
			self._throw_no_mappable_items(order, order_id)

		taxes = self._get_order_taxes(order, items)
		company_name = (
			company or self.settings.get("company") or frappe.defaults.get_global_default("company")
		)
		if not company_name:
			frappe.throw(_("Set Company on Medusa Settings before syncing orders."))

		transaction_date = getdate(order.get("created_at")) or nowdate()
		currency, conversion_rate = self._order_currency_and_rate(order, company_name, transaction_date)

		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": self.settings.get("sales_order_series") or "SO-MED-",
				ORDER_ID_FIELD: order_id,
				ORDER_NUMBER_FIELD: cstr(
					order.get("display_id") or order.get("custom_display_id") or order_id
				),
				ORDER_STATUS_FIELD: status_label(order),
				"customer": customer,
				"transaction_date": transaction_date,
				"delivery_date": delivery_date,
				"company": company_name,
				"currency": currency,
				"conversion_rate": conversion_rate,
				"price_list_currency": currency,
				"plc_conversion_rate": conversion_rate,
				"selling_price_list": self.settings.get("price_list") or get_dummy_price_list(),
				"ignore_pricing_rule": 1,
				"items": items,
				"taxes": taxes,
				"tax_category": get_dummy_tax_category(),
				"customer_address": addresses.get("billing_address"),
				"shipping_address_name": addresses.get("shipping_address"),
			}
		)

		cost_center = self.settings.get("cost_center")
		if cost_center:
			for row in so.items:
				row.cost_center = cost_center

		so.flags.ignore_mandatory = True
		so.flags.medusa_order_json = json.dumps(order, default=str)
		so.save()
		so.submit()

		metadata = order.get("metadata")
		note = metadata.get("note") if isinstance(metadata, dict) else None
		if note:
			so.add_comment(text=f"Order Note: {note}")
		return so

	def cancel_if_safe(self, order: dict, sales_order_name: str) -> bool:
		"""Cancel SO only if no submitted Sales Invoice keeps it linked.
		Delivery Notes are assumed already cancelled by the caller."""
		order_id = cstr(order.get("id") or "")
		if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
			return False

		so = frappe.get_doc("Sales Order", sales_order_name)
		submitted_si = frappe.db.get_value(
			"Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name"
		)
		if not submitted_si and so.docstatus == 1:
			so.cancel()
			return True

		if so.docstatus == 1:
			frappe.db.set_value(
				"Sales Order",
				sales_order_name,
				ORDER_STATUS_FIELD,
				status_label(order),
				update_modified=False,
			)
		return False

	# ------------------------------------------------------------------ #
	# items
	# ------------------------------------------------------------------ #
	@staticmethod
	def _throw_no_mappable_items(order: dict, order_id: str) -> None:
		missing = [
			cstr(line.get("variant_sku") or line.get("title") or line.get("variant_id") or line.get("id"))
			for line in order.get("items") or []
			if isinstance(line, dict) and not resolve_item_code(line)
		]
		frappe.throw(
			_("No mappable line items for Medusa order {0}. Sync products first. Unmapped: {1}").format(
				order_id, ", ".join(missing) or "-"
			),
			title=_("Order Items Missing"),
		)

	def _get_order_items(self, line_items: list, delivery_date) -> list[dict]:
		warehouse = self.settings.get("warehouse")
		if not warehouse:
			frappe.throw(_("Set Default Warehouse on Medusa Settings before syncing orders."))

		cost_center = self.settings.get("cost_center")
		items: list[dict] = []
		for line in line_items or []:
			if not isinstance(line, dict):
				continue
			item_code = resolve_item_code(line)
			if not item_code:
				continue
			qty = flt(line.get("quantity") or 0)
			if qty <= 0:
				continue

			price_list_rate = self._line_gross_rate(line, qty)
			rate = self._line_net_rate(line, qty)
			discount_per_unit = max(price_list_rate - rate, 0)

			row: dict[str, Any] = {
				"item_code": item_code,
				"item_name": cstr(line.get("product_title") or line.get("title") or item_code)[:140],
				"description": cstr(line.get("subtitle") or line.get("variant_title") or "")[:140],
				"price_list_rate": price_list_rate,
				"rate": rate,
				"discount_amount": discount_per_unit,
				"delivery_date": delivery_date,
				"qty": qty,
				"stock_uom": "Nos",
				"uom": "Nos",
				"warehouse": warehouse,
			}
			if ORDER_ITEM_DISCOUNT_FIELD:
				row[ORDER_ITEM_DISCOUNT_FIELD] = discount_per_unit
			if cost_center:
				row["cost_center"] = cost_center
			items.append(row)
		return items

	@staticmethod
	def _line_gross_rate(line: dict, qty: float) -> float:
		"""Pre-discount, tax-exclusive unit price straight from Medusa."""
		unit = flt(line.get("unit_price"))
		if not unit and line.get("subtotal") is not None:
			unit = flt(line.get("subtotal")) / qty
		return max(unit, 0)

	@staticmethod
	def _line_net_rate(line: dict, qty: float) -> float:
		"""Post-discount, tax-exclusive unit price (Medusa subtotal/discount_total
		are always tax-exclusive, so tax is applied separately as its own tax row)."""
		if not qty:
			return 0
		subtotal = flt(line.get("subtotal")) or flt(line.get("unit_price")) * qty
		net_total = subtotal - flt(line.get("discount_total") or 0)
		return max(net_total / qty, 0)

	# ------------------------------------------------------------------ #
	# taxes / shipping
	# ------------------------------------------------------------------ #
	@staticmethod
	def _rate_pct(rate: float) -> float:
		"""Medusa sometimes reports rate as a fraction (0.18) vs a percent (18)."""
		rate = flt(rate)
		return rate * 100 if rate and rate <= 1 else rate

	@staticmethod
	def _tax_row(
		*,
		account_head: str,
		desc: str,
		rate_pct: float,
		amount: float,
		cost_center: str | None,
		item_wise_detail: dict | None = None,
	) -> dict:
		"""Single place that builds a Sales Taxes and Charges row, used for both
		line-item tax lines and shipping-method tax lines."""
		return {
			"charge_type": "Actual",
			"account_head": account_head,
			"description": f"{desc} - {rate_pct:.2f}%" if rate_pct else desc,
			"tax_amount": amount,
			"cost_center": cost_center,
			"item_wise_tax_detail": item_wise_detail or {},
			"dont_recompute_tax": 1,
		}

	def _tax_rows_from_lines(
		self, tax_lines: list, *, account_head: str, item_code: str | None
	) -> list[dict]:
		"""Shared by item-level and shipping-level tax_lines arrays."""
		rows = []
		for tax in tax_lines or []:
			if not isinstance(tax, dict):
				continue
			amount = flt(tax.get("total") or tax.get("amount") or tax.get("subtotal") or 0)
			if not amount:
				continue
			rate_pct = self._rate_pct(tax.get("rate") or 0)
			desc = cstr(tax.get("code") or tax.get("name") or tax.get("description") or "Tax")
			item_wise_detail = {item_code: [rate_pct, amount]} if item_code else {}
			rows.append(
				self._tax_row(
					account_head=account_head,
					desc=desc,
					rate_pct=rate_pct,
					amount=amount,
					cost_center=self.settings.get("cost_center"),
					item_wise_detail=item_wise_detail,
				)
			)
		return rows

	def _get_order_taxes(self, order: dict, items: list[dict]) -> list[dict]:
		tax_account = self._get_tax_account(charge_type="sales_tax")
		taxes: list[dict] = []
		for line in order.get("items") or []:
			if not isinstance(line, dict):
				continue
			item_code = resolve_item_code(line)
			taxes.extend(
				self._tax_rows_from_lines(
					line.get("tax_lines"), account_head=tax_account, item_code=item_code
				)
			)

		self._add_shipping_charges(taxes, order, items)

		if cint(self.settings.get("consolidate_taxes")):
			taxes = self._consolidate_taxes(taxes)

		for row in taxes:
			if isinstance(row.get("item_wise_tax_detail"), dict):
				row["item_wise_tax_detail"] = json.dumps(row["item_wise_tax_detail"])
		return taxes

	def _get_tax_account(self, *, charge_type: str) -> str:
		account = (
			self.settings.get("default_shipping_charges_account")
			if charge_type == "shipping"
			else self.settings.get("default_sales_tax_account")
		)
		if not account:
			frappe.throw(
				_(
					"Set Default Sales Tax Account / Default Shipping Charges Account on Medusa Settings "
					"before syncing orders with tax or shipping lines."
				),
				title=_("Tax Account Required"),
			)
		return account

	def _add_shipping_charges(self, taxes: list, order: dict, items: list) -> None:
		shipping_item = self.settings.get("shipping_item")
		shipping_as_item = bool(cint(self.settings.get("add_shipping_as_item")) and shipping_item)
		methods = order.get("shipping_methods") or []

		for method in methods:
			if not isinstance(method, dict):
				continue
			shipping_option = method.get("shipping_option") or {}
			amount = flt(
				method.get("total")
				or method.get("amount")
				or method.get("subtotal")
				or shipping_option.get("amount")
				or 0
			)
			if amount <= 0:
				continue

			title = cstr(method.get("name") or shipping_option.get("name") or "Shipping")
			self._append_shipping_charge(taxes, items, shipping_as_item, amount, title)

			tax_account = self._get_tax_account(charge_type="sales_tax")
			item_wise_detail_code = shipping_item if shipping_as_item else None
			shipping_tax_rows = self._tax_rows_from_lines(
				method.get("tax_lines"), account_head=tax_account, item_code=item_wise_detail_code
			)
			for row in shipping_tax_rows:
				row["description"] = row["description"].replace("Tax", "Shipping Tax", 1)
			taxes.extend(shipping_tax_rows)

		if not methods and flt(order.get("shipping_total") or 0) > 0:
			self._append_shipping_charge(
				taxes, items, shipping_as_item, flt(order.get("shipping_total")), "Shipping"
			)

	def _append_shipping_charge(self, taxes, items, shipping_as_item, amount, title) -> None:
		if shipping_as_item:
			items.append(
				{
					"item_code": self.settings.shipping_item,
					"item_name": title,
					"rate": amount,
					"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
					"qty": 1,
					"stock_uom": "Nos",
					"uom": "Nos",
					"warehouse": self.settings.get("warehouse"),
				}
			)
		else:
			taxes.append(
				self._tax_row(
					account_head=self._get_tax_account(charge_type="shipping"),
					desc=title,
					rate_pct=0,
					amount=amount,
					cost_center=self.settings.get("cost_center"),
				)
			)

	@staticmethod
	def _consolidate_taxes(taxes: list[dict]) -> list[dict]:
		by_account: dict[str, dict] = {}
		for tax in taxes:
			account = tax["account_head"]
			row = by_account.setdefault(
				account,
				{
					"charge_type": "Actual",
					"account_head": account,
					"description": tax.get("description"),
					"cost_center": tax.get("cost_center"),
					"included_in_print_rate": 0,
					"dont_recompute_tax": 1,
					"tax_amount": 0,
					"item_wise_tax_detail": {},
				},
			)
			row["tax_amount"] += flt(tax.get("tax_amount"))
			detail = tax.get("item_wise_tax_detail") or {}
			if isinstance(detail, dict):
				row["item_wise_tax_detail"].update(detail)
		return list(by_account.values())

	# ------------------------------------------------------------------ #
	# currency
	# ------------------------------------------------------------------ #
	@staticmethod
	def _order_currency_and_rate(order: dict, company: str, transaction_date) -> tuple[str, float]:
		"""No silent fallback: missing Currency Exchange record fails the sync."""
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		order_currency = cstr(order.get("currency_code") or company_currency).upper()
		if order_currency == company_currency:
			return order_currency, 1.0

		from erpnext.setup.utils import get_exchange_rate

		rate = flt(get_exchange_rate(order_currency, company_currency, transaction_date))
		if not rate:
			frappe.throw(
				_(
					"No Currency Exchange rate found for {0} → {1} on {2}. Add one before syncing this order."
				).format(order_currency, company_currency, transaction_date)
			)
		return order_currency, rate
