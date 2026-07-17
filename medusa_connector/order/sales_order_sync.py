# Copyright (c) 2026, Aerele Technologies and contributors
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
	ORDER_LINE_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
)
from medusa_connector.order._shared import resolve_item_code, status_label


class SalesOrderSync:
	def __init__(self, settings):
		self.settings = settings

	@staticmethod
	def get_sales_order_doc(order_id: str):
		name = frappe.db.get_value(
			"Sales Order",
			{ORDER_ID_FIELD: cstr(order_id)},
			"name",
		)
		return frappe.get_doc("Sales Order", name) if name else None

	def ensure_items(self, order: dict) -> None:
		"""Sync Medusa products that are not yet mapped to an ERPNext Item."""
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

		for product_id in unmapped_ids:
			product = product_service.get_product(product_id)
			if product:
				product_sync.sync(product_mapper.map(product), force=True)

	def create(
		self,
		order: dict,
		*,
		customer: str,
		addresses: dict,
		company: str | None = None,
	):
		"""Build and submit the Sales Order. Caller (OrderSync) logs failures."""
		order_id = cstr(order.get("id"))
		transaction_date = getdate(order.get("created_at")) or nowdate()

		items = self._get_order_items(
			order.get("items") or [],
			transaction_date,
		)

		if not items:
			self._throw_no_mappable_items(order, order_id)

		company_name = company or self.settings.get("company")

		if not company_name:
			frappe.throw(_("Set Company on Medusa Settings before syncing orders."))

		taxes = self._get_order_taxes(order, items)

		currency, conversion_rate = self._order_currency_and_rate(
			order,
			company_name,
			transaction_date,
		)

		so = frappe.get_doc(
			self._build_sales_order_data(
				order=order,
				order_id=order_id,
				customer=customer,
				addresses=addresses,
				company=company_name,
				transaction_date=transaction_date,
				delivery_date=transaction_date,
				currency=currency,
				conversion_rate=conversion_rate,
				items=items,
				taxes=taxes,
			)
		)

		self._apply_cost_center(so)

		so.flags.ignore_mandatory = True

		so.save()
		self._validate_medusa_total(so, order)
		so.submit()

		self._add_order_note(so, order)

		return so

	def cancel_if_safe(self, order: dict, sales_order_name: str) -> bool:
		"""Cancel SO only if no submitted Sales Invoice keeps it linked."""
		order_id = cstr(order.get("id") or "")

		if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
			return False

		so = frappe.get_doc("Sales Order", sales_order_name)

		submitted_si = frappe.db.get_value(
			"Sales Invoice",
			{ORDER_ID_FIELD: order_id, "docstatus": 1},
			"name",
		)

		canceled = not submitted_si and so.docstatus == 1

		if canceled:
			so.cancel()

		frappe.db.set_value(
			"Sales Order",
			sales_order_name,
			ORDER_STATUS_FIELD,
			status_label(order),
			update_modified=False,
		)

		return canceled

	def _build_sales_order_data(
		self,
		*,
		order: dict,
		order_id: str,
		customer: str,
		addresses: dict,
		company: str,
		transaction_date,
		delivery_date,
		currency: str,
		conversion_rate: float,
		items: list[dict],
		taxes: list[dict],
	) -> dict:
		"""Build Sales Order document data."""
		return {
			"doctype": "Sales Order",
			"naming_series": self.settings.get("sales_order_series") or "SO-MED-",
			ORDER_ID_FIELD: order_id,
			ORDER_NUMBER_FIELD: cstr(order.get("display_id") or order.get("custom_display_id") or order_id),
			ORDER_STATUS_FIELD: status_label(order),
			"customer": customer,
			"transaction_date": transaction_date,
			"delivery_date": delivery_date,
			"company": company,
			"currency": currency,
			"conversion_rate": conversion_rate,
			"price_list_currency": currency,
			"plc_conversion_rate": conversion_rate,
			"selling_price_list": (self.settings.get("price_list") or get_dummy_price_list()),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
			"customer_address": addresses.get("billing_address"),
			"shipping_address_name": addresses.get("shipping_address"),
		}

	def _apply_cost_center(self, so) -> None:
		cost_center = self.settings.get("cost_center")

		if cost_center:
			for item in so.items:
				item.cost_center = cost_center

	@staticmethod
	def _add_order_note(so, order: dict) -> None:
		metadata = order.get("metadata")
		note = metadata.get("note") if isinstance(metadata, dict) else None

		if note:
			so.add_comment(text=f"Order Note: {note}")

	@staticmethod
	def _throw_no_mappable_items(order: dict, order_id: str) -> None:
		missing = [
			cstr(line.get("variant_sku") or line.get("title") or line.get("variant_id") or line.get("id"))
			for line in order.get("items") or []
			if isinstance(line, dict) and not resolve_item_code(line)
		]

		frappe.throw(
			_("No mappable line items for Medusa order {0}. Sync products first. Unmapped: {1}").format(
				order_id,
				", ".join(missing) or "-",
			),
			title=_("Order Items Missing"),
		)

	def _get_order_items(
		self,
		line_items: list,
		delivery_date,
	) -> list[dict]:
		warehouse = self.settings.get("warehouse")

		if not warehouse:
			frappe.throw(_("Set Default Warehouse on Medusa Settings before syncing orders."))

		cost_center = self.settings.get("cost_center")
		items = []

		for line in line_items or []:
			if not isinstance(line, dict):
				continue

			item = self._build_order_item(
				line,
				delivery_date,
				warehouse,
				cost_center,
			)

			if item:
				items.append(item)

		return items

	def _build_order_item(
		self,
		line: dict,
		delivery_date,
		warehouse: str,
		cost_center: str | None,
	) -> dict | None:
		item_code = resolve_item_code(line)
		qty = flt(line.get("quantity"))

		if not item_code or qty <= 0:
			return None

		price_list_rate, rate, discount_amount = self._line_rates(line, qty)
		discount_percentage = (discount_amount / price_list_rate * 100) if price_list_rate else 0

		item: dict[str, Any] = {
			"item_code": item_code,
			ORDER_LINE_ID_FIELD: cstr(line.get("id") or ""),
			"item_name": cstr(line.get("product_title") or line.get("title") or item_code)[:140],
			"description": cstr(line.get("subtitle") or line.get("variant_title") or "")[:140],
			"price_list_rate": price_list_rate,
			"rate": rate,
			"discount_amount": discount_amount,
			"discount_percentage": discount_percentage,
			"delivery_date": delivery_date,
			"qty": qty,
			"stock_uom": "Nos",
			"uom": "Nos",
			"warehouse": warehouse,
		}

		if ORDER_ITEM_DISCOUNT_FIELD:
			item[ORDER_ITEM_DISCOUNT_FIELD] = discount_amount

		if cost_center:
			item["cost_center"] = cost_center

		return item

	def _validate_medusa_total(self, so, order: dict) -> None:
		expected = flt(order.get("total"))
		if expected and abs(flt(so.grand_total) - expected) > 0.01:
			frappe.throw(
				_("Medusa total {0} does not match ERPNext total {1}. The order was not submitted.").format(
					expected, so.grand_total
				),
				title=_("Order Total Mismatch"),
			)

	@staticmethod
	def _line_rates(line: dict, qty: float) -> tuple[float, float, float]:
		"""Per-unit (price_list_rate, rate, discount_amount) for a line."""
		price_list_rate = flt(line.get("subtotal")) / qty
		discount_subtotal = line.get("discount_subtotal")
		if discount_subtotal is None:
			discount_subtotal = sum(
				flt(row.get("amount")) for row in line.get("adjustments") or [] if isinstance(row, dict)
			)
		discount_amount = flt(discount_subtotal) / qty
		rate = price_list_rate - discount_amount

		return price_list_rate, rate, discount_amount

	@staticmethod
	def _tax_row(
		*,
		account_head: str,
		desc: str,
		amount: float,
		cost_center: str | None,
		item_wise_detail: dict | None = None,
	) -> dict:
		return {
			"charge_type": "Actual",
			"account_head": account_head,
			"description": desc,
			"tax_amount": flt(amount),
			"cost_center": cost_center,
			"item_wise_tax_detail": item_wise_detail or {},
			"dont_recompute_tax": 1,
		}

	def _tax_rows_from_lines(
		self,
		tax_lines: list,
		*,
		account_head: str,
		item_code: str | None,
	) -> list[dict]:
		rows = []

		for tax in tax_lines or []:
			if not isinstance(tax, dict):
				continue

			amount = flt(tax.get("total") if tax.get("total") is not None else tax.get("amount"))
			rate = flt(tax.get("rate"))

			description = cstr(tax.get("code") or tax.get("name") or tax.get("description") or "Tax")

			item_detail = {}

			if item_code:
				item_detail[item_code] = [rate, amount]

			rows.append(
				self._tax_row(
					account_head=account_head,
					desc=description,
					amount=amount,
					cost_center=self.settings.get("cost_center"),
					item_wise_detail=item_detail,
				)
			)

		return rows

	@staticmethod
	def _consolidate_taxes(
		taxes: list[dict],
	) -> list[dict]:
		grouped = {}

		for tax in taxes:
			key = (
				tax["account_head"],
				tax["charge_type"],
			)

			row = grouped.setdefault(
				key,
				{
					"charge_type": tax["charge_type"],
					"account_head": tax["account_head"],
					"description": tax.get("description"),
					"cost_center": tax.get("cost_center"),
					"dont_recompute_tax": 1,
					"tax_amount": 0.0,
					"item_wise_tax_detail": {},
				},
			)

			row["tax_amount"] = flt(row.get("tax_amount")) + flt(tax.get("tax_amount"))

			detail = tax.get("item_wise_tax_detail") or {}

			if isinstance(detail, dict):
				for item_code, values in detail.items():
					if item_code in row["item_wise_tax_detail"]:
						previous = row["item_wise_tax_detail"][item_code]
						row["item_wise_tax_detail"][item_code] = [
							flt(previous[0]) + flt(values[0]),
							flt(previous[1]) + flt(values[1]),
						]
					else:
						row["item_wise_tax_detail"][item_code] = values

		return list(grouped.values())

	def _get_order_taxes(
		self,
		order: dict,
		items: list[dict],
	) -> list[dict]:
		taxes = []

		self._add_item_taxes(taxes, order)
		self._add_shipping_charges(taxes, order, items)

		if cint(self.settings.get("consolidate_taxes")):
			taxes = self._consolidate_taxes(taxes)

		for row in taxes:
			if isinstance(row.get("item_wise_tax_detail"), dict):
				row["item_wise_tax_detail"] = json.dumps(row["item_wise_tax_detail"])

		return taxes

	def _add_item_taxes(
		self,
		taxes: list[dict],
		order: dict,
	) -> None:
		for line in order.get("items") or []:
			if not isinstance(line, dict):
				continue

			item_code = resolve_item_code(line)

			if not item_code:
				continue

			for tax_line in line.get("tax_lines") or []:
				if not isinstance(tax_line, dict):
					continue

				tax_rate_id = cstr(tax_line.get("tax_rate_id"))

				taxes.extend(
					self._tax_rows_from_lines(
						[tax_line],
						account_head=self._get_tax_account_for_rate(
							tax_rate_id=tax_rate_id,
							charge_type="sales_tax",
						),
						item_code=item_code,
					)
				)

	def _get_tax_account(
		self,
		*,
		charge_type: str,
	) -> str:
		account = (
			self.settings.get("default_shipping_charges_account")
			if charge_type == "shipping"
			else self.settings.get("default_sales_tax_account")
		)

		if not account:
			frappe.throw(
				_(
					"Set Default Sales Tax Account / Default Shipping "
					"Charges Account on Medusa Settings before syncing "
					"orders with tax or shipping lines."
				),
				title=_("Tax Account Required"),
			)

		return account

	def _get_tax_account_for_rate(
		self,
		*,
		tax_rate_id: str,
		charge_type: str,
	) -> str:
		account = frappe.db.get_value(
			"Medusa Account Mapping",
			{
				"parent": "Medusa Settings",
				"medusa_tax_or_shipping_id": tax_rate_id,
			},
			"erpnext_account",
		)

		return account or self._get_tax_account(charge_type=charge_type)

	def _add_shipping_charges(
		self,
		taxes: list,
		order: dict,
		items: list,
	) -> None:
		shipping_item = self.settings.get("shipping_item")

		shipping_as_item = bool(cint(self.settings.get("add_shipping_as_item")) and shipping_item)

		for method in order.get("shipping_methods") or []:
			if not isinstance(method, dict):
				continue

			if shipping_as_item:
				self._add_shipping_as_item(
					taxes,
					items,
					method,
					shipping_item,
				)
			else:
				self._add_shipping_as_charge(
					taxes,
					method,
				)

	def _add_shipping_as_item(
		self,
		taxes: list,
		items: list,
		method: dict,
		shipping_item: str,
	) -> None:
		price_list_rate = flt(method.get("subtotal"))
		discount_amount = flt(method.get("discount_subtotal"))
		rate = price_list_rate - discount_amount

		items.append(
			{
				"item_code": shipping_item,
				"item_name": cstr(method.get("name") or "Shipping"),
				"price_list_rate": price_list_rate,
				"rate": rate,
				"discount_amount": discount_amount,
				"discount_percentage": ((discount_amount / price_list_rate * 100) if price_list_rate else 0),
				"delivery_date": (items[-1]["delivery_date"] if items else nowdate()),
				"qty": 1,
				"stock_uom": "Nos",
				"uom": "Nos",
				"warehouse": self.settings.get("warehouse"),
			}
		)

		for tax_line in method.get("tax_lines") or []:
			if not isinstance(tax_line, dict):
				continue

			tax_rate_id = cstr(tax_line.get("tax_rate_id"))

			taxes.extend(
				self._tax_rows_from_lines(
					[tax_line],
					account_head=self._get_tax_account_for_rate(
						tax_rate_id=tax_rate_id,
						charge_type="sales_tax",
					),
					item_code=shipping_item,
				)
			)

	def _add_shipping_as_charge(
		self,
		taxes: list,
		method: dict,
	) -> None:
		amount = flt(method.get("subtotal")) - flt(method.get("discount_subtotal"))

		taxes.append(
			self._tax_row(
				account_head=self._get_tax_account_for_rate(
					tax_rate_id=cstr(method.get("shipping_option_id")),
					charge_type="shipping",
				),
				desc=cstr(method.get("name") or "Shipping"),
				amount=amount,
				cost_center=self.settings.get("cost_center"),
			)
		)

		for tax_line in method.get("tax_lines") or []:
			if not isinstance(tax_line, dict):
				continue

			tax_rate_id = cstr(tax_line.get("tax_rate_id"))

			taxes.extend(
				self._tax_rows_from_lines(
					[tax_line],
					account_head=self._get_tax_account_for_rate(
						tax_rate_id=tax_rate_id,
						charge_type="sales_tax",
					),
					item_code=None,
				)
			)

	@staticmethod
	def _order_currency_and_rate(
		order: dict,
		company: str,
		transaction_date,
	) -> tuple[str, float]:
		company_currency = frappe.get_cached_value(
			"Company",
			company,
			"default_currency",
		)

		order_currency = cstr(order.get("currency_code") or company_currency).upper()

		if order_currency == company_currency:
			return order_currency, 1.0

		from erpnext.setup.utils import get_exchange_rate

		rate = flt(
			get_exchange_rate(
				order_currency,
				company_currency,
				transaction_date,
			)
		)

		if not rate:
			frappe.throw(
				_(
					"No Currency Exchange rate found for {0} → {1} on {2}. Add one before syncing this order."
				).format(
					order_currency,
					company_currency,
					transaction_date,
				)
			)

		return order_currency, rate
