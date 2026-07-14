# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa → ERPNext Sales Order sync (Shopify / Ecommerce Core pattern).

Flow (same as Shopify ``sync_sales_order``):
  1. Skip if Sales Order already exists for ``medusa_order_id``
  2. ``ensure_order_customer`` (EcommerceCustomer for Customer / Address / Contact)
  3. Ensure line items are mapped (import product if missing)
  4. Create and submit Sales Order
  5. Optionally create Sales Invoice when payment is captured

Logging uses **Ecommerce Integration Log** via ``create_medusa_log``.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from ecommerce_core.utils.price_list import get_dummy_price_list
from ecommerce_core.utils.taxation import get_dummy_tax_category
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate

from medusa_connector.constants import (
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.customer.sync import ensure_order_customer
from medusa_connector.product.item_mapping import get_erpnext_item
from medusa_connector.utils.logging import create_medusa_log


def sync_sales_order(order: dict, request_id: str | None = None) -> str | None:
	"""Create an ERPNext Sales Order from a Medusa order payload.

	Idempotent on ``medusa_order_id``. Safe for webhooks and bulk/old-order sync.
	Returns Sales Order name on success, ``None`` when skipped/failed.
	"""
	frappe.set_user("Administrator")
	if request_id:
		frappe.flags.request_id = request_id

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled:
		create_medusa_log(
			status="Invalid",
			message=_("Medusa Connector is disabled; order not synced."),
			request_data=order,
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return None

	order_id = cstr(order.get("id") or "")
	if not order_id:
		create_medusa_log(
			status="Invalid",
			message=_("Medusa order payload has no id."),
			request_data=order,
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return None

	existing = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}, "name")
	if existing:
		# Refresh status field if present; do not recreate.
		_update_order_status_fields(order)
		create_medusa_log(
			status="Invalid",
			message=_("Sales Order {0} already exists for Medusa order {1}.").format(existing, order_id),
			request_data={"order_id": order_id, "sales_order": existing},
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return existing

	try:
		ensure_order_customer(order, settings=settings)
		ensure_order_items(order)
		so = create_order(order, settings)
		create_medusa_log(
			status="Success",
			message=_("Created Sales Order {0} for Medusa order {1}.").format(
				so.name if so else "-", order_id
			),
			request_data={"order_id": order_id, "display_id": order.get("display_id")},
			response_data={"sales_order": so.name if so else None},
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return so.name if so else None
	except Exception as exc:
		create_medusa_log(
			status="Error",
			exception=exc,
			rollback=True,
			request_data=order,
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return None


def create_order(order: dict, settings, company: str | None = None):
	"""Create Sales Order (and optional Sales Invoice when paid)."""
	from medusa_connector.order.invoice import create_sales_invoice

	so = create_sales_order(order, settings, company=company)
	if not so:
		return None

	if _is_paid(order) and cint(settings.get("sync_sales_invoice")):
		create_sales_invoice(order, settings, so)

	return so


def create_sales_order(order: dict, settings, company: str | None = None):
	"""Build and submit a Sales Order for the Medusa order."""
	order_id = cstr(order.get("id"))
	existing = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}, "name")
	if existing:
		return frappe.get_doc("Sales Order", existing)

	customer = _resolve_customer_name(order, settings)
	delivery_date = getdate(order.get("created_at")) or nowdate()
	items = get_order_items(order.get("items") or [], settings, delivery_date)
	if not items:
		missing = _missing_item_labels(order.get("items") or [])
		frappe.throw(
			_("No mappable line items for Medusa order {0}. Sync products first. Unmapped: {1}").format(
				order_id, ", ".join(missing) or "-"
			),
			title=_("Order Items Missing"),
		)

	taxes = get_order_taxes(order, settings, items)
	company_name = company or settings.get("company") or frappe.defaults.get_global_default("company")
	if not company_name:
		frappe.throw(_("Set Company on Medusa Settings before syncing orders."))

	transaction_date = getdate(order.get("created_at")) or nowdate()
	currency, conversion_rate = _order_currency_and_rate(order, company_name, transaction_date)

	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"naming_series": settings.get("sales_order_series") or "SO-MED-",
			ORDER_ID_FIELD: order_id,
			ORDER_NUMBER_FIELD: cstr(order.get("display_id") or order.get("custom_display_id") or order_id),
			ORDER_STATUS_FIELD: _status_label(order),
			"customer": customer,
			"transaction_date": transaction_date,
			"delivery_date": delivery_date,
			"company": company_name,
			"currency": currency,
			"conversion_rate": conversion_rate,
			"price_list_currency": currency,
			"plc_conversion_rate": conversion_rate,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
			"customer_address": _find_customer_address(customer, "Billing"),
			"shipping_address_name": _find_customer_address(customer, "Shipping"),
		}
	)

	if settings.get("cost_center"):
		for row in so.items:
			row.cost_center = settings.cost_center

	if company:
		so.update({"company": company, "status": "Draft"})

	so.flags.ignore_mandatory = True
	so.flags.medusa_order_json = json.dumps(order, default=str)
	so.save(ignore_permissions=True)
	so.submit()

	note = (order.get("metadata") or {}).get("note") if isinstance(order.get("metadata"), dict) else None
	if note:
		so.add_comment(text=f"Order Note: {note}")

	return so


def get_order_items(line_items: list, settings, delivery_date) -> list[dict]:
	"""Map Medusa line items → Sales Order Item rows."""
	items: list[dict] = []
	warehouse = settings.get("warehouse")
	if not warehouse:
		frappe.throw(_("Set Default Warehouse on Medusa Settings before syncing orders."))

	for line in line_items or []:
		if not isinstance(line, dict):
			continue
		item_code = get_item_code(line)
		if not item_code:
			continue

		qty = flt(line.get("quantity") or 0)
		if qty <= 0:
			continue

		rate = _line_unit_rate(line)
		discount = flt(line.get("discount_total") or 0)
		per_unit_discount = discount / qty if qty else 0

		row: dict[str, Any] = {
			"item_code": item_code,
			"item_name": cstr(line.get("product_title") or line.get("title") or item_code)[:140],
			"description": cstr(line.get("subtitle") or line.get("variant_title") or "")[:140],
			"rate": rate,
			"delivery_date": delivery_date,
			"qty": qty,
			"stock_uom": "Nos",
			"warehouse": warehouse,
			"uom": "Nos",
		}
		if ORDER_ITEM_DISCOUNT_FIELD:
			row[ORDER_ITEM_DISCOUNT_FIELD] = per_unit_discount
		if settings.get("cost_center"):
			row["cost_center"] = settings.cost_center
		items.append(row)

	return items


def get_item_code(line_item: dict) -> str | None:
	"""Resolve ERPNext item from Medusa line (variant id / SKU)."""
	variant_id = cstr(
		line_item.get("variant_id")
		or ((line_item.get("variant") or {}).get("id") if isinstance(line_item.get("variant"), dict) else "")
		or ""
	)
	sku = cstr(
		line_item.get("variant_sku")
		or ((line_item.get("variant") or {}).get("sku") if isinstance(line_item.get("variant"), dict) else "")
		or line_item.get("sku")
		or ""
	)
	item = get_erpnext_item(variant_id=variant_id or None, sku=sku or None)
	if item:
		return item.name
	if sku and frappe.db.exists("Item", sku):
		return sku
	return None


def ensure_order_items(order: dict) -> None:
	"""Import missing Medusa products for order lines (Shopify create_items_if_not_exist)."""
	from medusa_connector.medusa.product import ProductService
	from medusa_connector.product.mapper import ProductMapper
	from medusa_connector.product.sync import ProductSync

	service = ProductService()
	mapper = ProductMapper()
	sync = ProductSync()
	seen: set[str] = set()

	for line in order.get("items") or []:
		if not isinstance(line, dict):
			continue
		if get_item_code(line):
			continue
		product_id = cstr(line.get("product_id") or "")
		if not product_id or product_id in seen:
			continue
		seen.add(product_id)
		try:
			product = service.get_product(product_id)
			if not product:
				continue
			mapped = mapper.map(product)
			sync.sync(mapped, force=True)
		except Exception as exc:
			frappe.logger("medusa_connector").warning(
				f"Could not auto-import product {product_id} for order: {exc}"
			)


def get_order_taxes(order: dict, settings, items: list[dict]) -> list[dict]:
	"""Build Sales Taxes and Charges rows from line and shipping tax lines."""
	taxes: list[dict] = []

	for line in order.get("items") or []:
		if not isinstance(line, dict):
			continue
		item_code = get_item_code(line)
		for tax in line.get("tax_lines") or []:
			if not isinstance(tax, dict):
				continue
			amount = flt(tax.get("total") or tax.get("amount") or tax.get("subtotal") or 0)
			if not amount:
				continue
			rate = flt(tax.get("rate") or 0)
			# Medusa rates are often 0-1 fractions
			rate_pct = rate * 100 if rate and rate <= 1 else rate
			desc = cstr(tax.get("code") or tax.get("name") or tax.get("description") or "Tax")
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account(tax, charge_type="sales_tax", settings=settings),
					"description": f"{desc} - {rate_pct:.2f}%" if rate_pct else desc,
					"tax_amount": amount,
					"included_in_print_rate": 0,
					"cost_center": settings.get("cost_center"),
					"item_wise_tax_detail": {item_code: [rate_pct, amount]} if item_code else {},
					"dont_recompute_tax": 1,
				}
			)

	_add_shipping_charges(taxes, order, settings, items)

	if cint(settings.get("consolidate_taxes")):
		taxes = _consolidate_taxes(taxes)

	for row in taxes:
		detail = row.get("item_wise_tax_detail")
		if isinstance(detail, dict):
			row["item_wise_tax_detail"] = json.dumps(detail)

	return taxes


def get_tax_account(tax: dict, *, charge_type: str, settings) -> str:
	"""Resolve ERPNext tax/charge account (defaults on Medusa Settings)."""
	account = None
	if charge_type == "shipping":
		account = settings.get("default_shipping_charges_account")
	else:
		account = settings.get("default_sales_tax_account")

	if not account:
		frappe.throw(
			_(
				"Set Default Sales Tax Account / Default Shipping Charges Account on Medusa Settings "
				"before syncing orders with tax or shipping lines."
			),
			title=_("Tax Account Required"),
		)
	return account


def cancel_order(order: dict, request_id: str | None = None) -> str | None:
	"""Cancel Sales Order when Medusa order is canceled (if not invoiced/delivered)."""
	frappe.set_user("Administrator")
	if request_id:
		frappe.flags.request_id = request_id

	order_id = cstr(order.get("id") or "")
	try:
		so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}, "name")
		if not so_name:
			create_medusa_log(
				status="Invalid",
				message=_("Sales Order does not exist for Medusa order {0}.").format(order_id),
				request_data={"order_id": order_id},
				method="medusa_connector.order.sync.cancel_order",
			)
			return None

		so = frappe.get_doc("Sales Order", so_name)
		status_label = _status_label(order)

		si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id}, "name")
		dns = frappe.get_all("Delivery Note", filters={ORDER_ID_FIELD: order_id}, pluck="name")

		if si:
			frappe.db.set_value("Sales Invoice", si, ORDER_STATUS_FIELD, status_label)
		for dn in dns:
			frappe.db.set_value("Delivery Note", dn, ORDER_STATUS_FIELD, status_label)

		if not si and not dns and so.docstatus == 1:
			so.cancel()
			msg = _("Cancelled Sales Order {0}.").format(so_name)
		else:
			frappe.db.set_value("Sales Order", so_name, ORDER_STATUS_FIELD, status_label)
			msg = _("Updated status on Sales Order {0} (linked docs prevent cancel).").format(so_name)

		create_medusa_log(
			status="Success",
			message=msg,
			request_data={"order_id": order_id},
			method="medusa_connector.order.sync.cancel_order",
		)
		return so_name
	except Exception as exc:
		create_medusa_log(
			status="Error",
			exception=exc,
			rollback=True,
			request_data=order,
			method="medusa_connector.order.sync.cancel_order",
		)
		return None


def get_sales_order(order_id: str):
	"""Return Sales Order doc for a Medusa order id, or None."""
	name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(order_id)}, "name")
	if name:
		return frappe.get_doc("Sales Order", name)
	return None


def sync_old_orders(
	*,
	from_date=None,
	to_date=None,
	force: bool = False,
) -> dict:
	"""Fetch Medusa orders in a date range and sync each (for future Sync Old Orders UI).

	Skips orders that already exist unless the caller re-runs after cancel/delete.
	"""
	from medusa_connector.medusa.order import OrderService

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled:
		frappe.throw(_("Enable the Medusa Connector first."))

	created_at_gt = None
	created_at_lt = None
	if from_date:
		created_at_gt = get_datetime(from_date).isoformat()
	if to_date:
		created_at_lt = get_datetime(to_date).isoformat()

	service = OrderService()
	synced = 0
	skipped = 0
	failed = 0

	for order in service.iter_orders(created_at_gt=created_at_gt, created_at_lt=created_at_lt):
		order_id = cstr(order.get("id") or "")
		if not order_id:
			continue
		if not force and frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}):
			skipped += 1
			continue
		# Re-fetch full order for line items / addresses
		full = service.get_order(order_id) or order
		log = create_medusa_log(
			status="Queued",
			method="medusa_connector.order.sync.sync_sales_order",
			message=_("Sync old order {0}").format(order_id),
			request_data={"order_id": order_id},
			make_new=True,
		)
		result = sync_sales_order(full, request_id=log.name)
		if result:
			synced += 1
		else:
			# Invalid (already exists) counts as skip; Error counted as failed via log
			if frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}):
				skipped += 1
			else:
				failed += 1

	return {"synced": synced, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _order_currency_and_rate(order: dict, company: str, transaction_date) -> tuple[str, float]:
	"""Order currency + conversion rate to company currency."""
	company_currency = frappe.get_cached_value("Company", company, "default_currency") or "INR"
	order_currency = cstr(order.get("currency_code") or company_currency).upper()
	if not order_currency:
		order_currency = company_currency
	if order_currency == company_currency:
		return order_currency, 1.0
	try:
		from erpnext.setup.utils import get_exchange_rate

		rate = flt(get_exchange_rate(order_currency, company_currency, transaction_date))
		if rate:
			return order_currency, rate
	except Exception:
		pass
	# Fall back to company currency so SO can still post when no exchange rate exists.
	frappe.logger("medusa_connector").warning(
		f"No Currency Exchange {order_currency}→{company_currency}; using company currency on SO"
	)
	return company_currency, 1.0


def _resolve_customer_name(order: dict, settings) -> str:
	"""Customer after ensure_order_customer; fall back to Default Customer."""
	from medusa_connector.constants import CUSTOMER_ID_FIELD

	customer_id = cstr(order.get("customer_id") or (order.get("customer") or {}).get("id") or "")
	if customer_id:
		name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")
		if name:
			return name
	default = settings.get("default_customer")
	if default and frappe.db.exists("Customer", default):
		return default
	frappe.throw(
		_("Could not resolve Customer for Medusa order {0}. Set Default Customer for guests.").format(
			order.get("id")
		)
	)


def _line_unit_rate(line: dict) -> float:
	"""Unit rate excluding per-unit discount; handle tax-inclusive lines."""
	qty = flt(line.get("quantity") or 1) or 1
	unit = flt(line.get("unit_price"))
	if not unit and line.get("subtotal") is not None:
		unit = flt(line.get("subtotal")) / qty

	discount = flt(line.get("discount_total") or 0)
	unit = unit - (discount / qty)

	if line.get("is_tax_inclusive") and line.get("tax_total"):
		unit = unit - (flt(line.get("tax_total")) / qty)

	return max(unit, 0)


def _missing_item_labels(line_items: list) -> list[str]:
	labels = []
	for line in line_items or []:
		if not isinstance(line, dict):
			continue
		if get_item_code(line):
			continue
		labels.append(
			cstr(line.get("variant_sku") or line.get("title") or line.get("variant_id") or line.get("id"))
		)
	return labels


def _add_shipping_charges(taxes: list, order: dict, settings, items: list) -> None:
	shipping_as_item = cint(settings.get("add_shipping_as_item")) and settings.get("shipping_item")
	for method in order.get("shipping_methods") or []:
		if not isinstance(method, dict):
			continue
		# Medusa Admin shipping method amounts
		amount = flt(
			method.get("total")
			or method.get("amount")
			or method.get("subtotal")
			or method.get("shipping_option", {}).get("amount")
			or 0
		)
		if amount <= 0:
			# Fall back to order-level shipping_total once
			continue

		title = cstr(method.get("name") or (method.get("shipping_option") or {}).get("name") or "Shipping")

		if shipping_as_item:
			items.append(
				{
					"item_code": settings.shipping_item,
					"item_name": title,
					"rate": amount,
					"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
					"qty": 1,
					"stock_uom": "Nos",
					"warehouse": settings.get("warehouse"),
					"uom": "Nos",
				}
			)
		else:
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account({}, charge_type="shipping", settings=settings),
					"description": title,
					"tax_amount": amount,
					"cost_center": settings.get("cost_center"),
					"dont_recompute_tax": 1,
				}
			)

		for tax in method.get("tax_lines") or []:
			if not isinstance(tax, dict):
				continue
			tax_amount = flt(tax.get("total") or tax.get("amount") or 0)
			if not tax_amount:
				continue
			rate = flt(tax.get("rate") or 0)
			rate_pct = rate * 100 if rate and rate <= 1 else rate
			desc = cstr(tax.get("code") or tax.get("name") or "Shipping Tax")
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account(tax, charge_type="sales_tax", settings=settings),
					"description": f"{desc} - {rate_pct:.2f}%" if rate_pct else desc,
					"tax_amount": tax_amount,
					"cost_center": settings.get("cost_center"),
					"item_wise_tax_detail": {settings.shipping_item: [rate_pct, tax_amount]}
					if shipping_as_item and settings.get("shipping_item")
					else {},
					"dont_recompute_tax": 1,
				}
			)

	# If no per-method shipping but order has shipping_total
	if not (order.get("shipping_methods") or []) and flt(order.get("shipping_total") or 0) > 0:
		amount = flt(order.get("shipping_total"))
		if shipping_as_item and settings.get("shipping_item"):
			items.append(
				{
					"item_code": settings.shipping_item,
					"item_name": "Shipping",
					"rate": amount,
					"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
					"qty": 1,
					"stock_uom": "Nos",
					"warehouse": settings.get("warehouse"),
					"uom": "Nos",
				}
			)
		else:
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account({}, charge_type="shipping", settings=settings),
					"description": "Shipping",
					"tax_amount": amount,
					"cost_center": settings.get("cost_center"),
					"dont_recompute_tax": 1,
				}
			)


def _consolidate_taxes(taxes: list[dict]) -> list[dict]:
	by_account: dict[str, dict] = {}
	for tax in taxes:
		account = tax["account_head"]
		by_account.setdefault(
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
		by_account[account]["tax_amount"] += flt(tax.get("tax_amount"))
		detail = tax.get("item_wise_tax_detail") or {}
		if isinstance(detail, dict):
			by_account[account]["item_wise_tax_detail"].update(detail)
	return list(by_account.values())


def _is_paid(order: dict) -> bool:
	status = cstr(order.get("payment_status") or "").lower()
	return status in {"captured", "paid"}


def _status_label(order: dict) -> str:
	parts = [
		cstr(order.get("status") or ""),
		cstr(order.get("payment_status") or ""),
		cstr(order.get("fulfillment_status") or ""),
	]
	return " / ".join(p for p in parts if p)[:140]


def update_order_status_fields(order: dict) -> None:
	"""Refresh Medusa status custom field on linked SO / SI."""
	order_id = cstr(order.get("id") or "")
	if not order_id:
		return
	label = _status_label(order)
	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}, "name")
	if so:
		frappe.db.set_value("Sales Order", so, ORDER_STATUS_FIELD, label, update_modified=False)
	si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id}, "name")
	if si:
		frappe.db.set_value("Sales Invoice", si, ORDER_STATUS_FIELD, label, update_modified=False)


# Back-compat alias used during development
_update_order_status_fields = update_order_status_fields


def _find_customer_address(customer: str, address_type: str) -> str | None:
	"""Resolve Address name linked to Customer (Dynamic Link + address_type)."""
	if not customer:
		return None
	rows = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		fields=["parent"],
	)
	for row in rows:
		if frappe.db.get_value("Address", row.parent, "address_type") == address_type:
			return row.parent
	return rows[0].parent if rows else None
