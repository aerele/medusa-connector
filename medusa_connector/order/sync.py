# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa → ERPNext order sync (Ecommerce Core).

Full lifecycle (not SO-only):

  1. Ensure Customer / Address / Contact and Items
  2. Create Sales Order if missing (idempotent on ``medusa_order_id``)
  3. Apply current Medusa state → SI/PE (paid), DN (fulfillments), cancel, status

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
	"""Ensure Sales Order exists and apply the full Medusa order lifecycle.

	Idempotent on ``medusa_order_id``. Safe for webhooks and bulk Sync Orders.
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
	created = False

	try:
		if existing:
			so = frappe.get_doc("Sales Order", existing)
		else:
			ensure_order_customer(order, settings=settings)
			ensure_order_items(order)
			so = create_sales_order(order, settings)
			if not so:
				return None
			created = True

		# Reload after create so per_billed / docstatus are current
		so = frappe.get_doc("Sales Order", so.name)
		lifecycle = apply_order_lifecycle(order, so, settings)

		create_medusa_log(
			status="Success",
			message=_("{0} Sales Order {1} for Medusa order {2}; lifecycle: {3}.").format(
				_("Created") if created else _("Synced"),
				so.name,
				order_id,
				_lifecycle_summary(lifecycle),
			),
			request_data={"order_id": order_id, "display_id": order.get("display_id")},
			response_data={
				"sales_order": so.name,
				"created": created,
				"lifecycle": lifecycle,
			},
			method="medusa_connector.order.sync.sync_sales_order",
		)
		return so.name
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
	"""Create Sales Order then apply payment / fulfillment / cancel lifecycle.

	Mirrors Shopify ``create_order`` for brand-new orders.
	"""
	so = create_sales_order(order, settings, company=company)
	if not so:
		return None
	apply_order_lifecycle(order, so, settings)
	return so


def apply_order_lifecycle(order: dict, sales_order, settings) -> dict[str, Any]:
	"""Project Medusa order state onto ERPNext documents linked to ``sales_order``.

	Does not recreate the Sales Order. Steps (each idempotent / best-effort):

	1. Refresh status custom fields (SO / SI / DN)
	2. If paid/captured → Sales Invoice + Payment Entry (settings)
	3. Fulfillments → Delivery Notes; canceled fulfillments → cancel DNs
	4. If order canceled → cancel SO when safe (else status only)
	5. Refunded / partially_refunded → status stamp (credit-note path pending)

	Returns a dict describing actions taken for logging.
	"""
	from medusa_connector.order.fulfillment import sync_fulfillments_for_order
	from medusa_connector.order.invoice import create_sales_invoice

	result: dict[str, Any] = {
		"sales_invoice": None,
		"delivery_notes": [],
		"canceled_fulfillments": [],
		"order_canceled": False,
		"payment_status": cstr(order.get("payment_status") or ""),
		"fulfillment_status": cstr(order.get("fulfillment_status") or ""),
		"order_status": cstr(order.get("status") or ""),
		"errors": [],
	}

	if not sales_order:
		return result

	order_id = cstr(order.get("id") or "")
	update_order_status_fields(order)

	# Cancelled ERPNext SO: only refresh status; do not create SI/DN.
	sales_order = frappe.get_doc("Sales Order", sales_order.name)
	if sales_order.docstatus == 2:
		result["skipped"] = "sales_order_cancelled"
		return result

	# --- Payment → SI + PE -------------------------------------------------
	if sales_order.docstatus == 1 and _is_paid(order) and cint(settings.get("sync_sales_invoice")):
		try:
			sales_order = frappe.get_doc("Sales Order", sales_order.name)
			payment_id = _primary_captured_payment_id(order)
			si_name = create_sales_invoice(order, settings, sales_order, payment_id=payment_id)
			result["sales_invoice"] = si_name
		except Exception as exc:
			result["errors"].append(f"invoice: {exc}")
			frappe.logger("medusa_connector").warning(f"Lifecycle SI failed for order {order_id}: {exc}")

	# --- Fulfillment / shipment → DN --------------------------------------
	if sales_order.docstatus == 1 and cint(settings.get("sync_delivery_note")):
		try:
			sales_order = frappe.get_doc("Sales Order", sales_order.name)
			ful_result = sync_fulfillments_for_order(order, settings, sales_order)
			result["delivery_notes"] = ful_result.get("delivery_notes") or []
			result["canceled_fulfillments"] = ful_result.get("canceled") or []
		except Exception as exc:
			result["errors"].append(f"fulfillment: {exc}")
			frappe.logger("medusa_connector").warning(f"Lifecycle DN failed for order {order_id}: {exc}")

	# --- Order cancellation -----------------------------------------------
	if _is_canceled(order):
		try:
			from medusa_connector.order.fulfillment import cancel_order_delivery_notes

			# Cancel Delivery Notes first (restore inventory), then the SO when allowed.
			# Quiet — bulk Sync Orders should not emit a second log per order.
			dn_result = cancel_order_delivery_notes(order_id, status_label=_status_label(order))
			result["canceled_delivery_notes"] = dn_result.get("canceled") or []
			canceled = _cancel_sales_order_if_safe(order, sales_order.name)
			result["order_canceled"] = canceled
			update_order_status_fields(order)
		except Exception as exc:
			result["errors"].append(f"cancel: {exc}")
			frappe.logger("medusa_connector").warning(f"Lifecycle cancel failed for order {order_id}: {exc}")

	# --- Refunds (status only until credit-note flow exists) --------------
	if _is_refunded(order):
		update_order_status_fields(order)
		result["refund_status"] = cstr(order.get("payment_status") or "refunded")

	return result


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
	"""Import missing Medusa products for order lines (auto product import for order lines)."""
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
	"""Handle ``order.canceled`` following ERPNext's standard document lifecycle.

	The Sales Order is the hub. On Medusa order cancellation we:

	1. Cancel every linked Delivery Note (restores inventory via ERPNext's
	   stock-ledger reversal), best-effort so one bad DN does not abort the rest.
	2. Cancel the Sales Order when ERPNext permits it — i.e. when no submitted
	   Sales Invoice keeps it linked. If a submitted SI exists it is left intact
	   (the separate ``payment.refunded`` workflow reverses SI + Payment Entry),
	   and the cancellation is recorded on the status field.

	This decouples order cancellation from payment refund, mirroring Medusa's
	separate ``order.canceled`` and ``payment.refunded`` events.
	"""
	from medusa_connector.order.fulfillment import cancel_order_delivery_notes

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
		payment_status = cstr(order.get("payment_status") or "")

		if payment_status == "refunded":
			from medusa_connector.order.refund import process_refund

			payment_id = _get_payment_id(order)

			if payment_id:
				process_refund(
					payment_id=payment_id,
					request_id=request_id,
				)

		status_label = _status_label(order)

		# 1) Cancel linked Delivery Notes first so inventory is restored and they
		#    no longer keep the Sales Order linked (ERPNext blocks SO cancel while
		#    submitted DNs exist).
		dn_result = cancel_order_delivery_notes(order_id, status_label=status_label, request_id=request_id)

		# 2) Refresh status on any submitted Sales Invoice (left for the refund flow).
		si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name")
		if si:
			frappe.db.set_value("Sales Invoice", si, ORDER_STATUS_FIELD, status_label, update_modified=False)

		# 3) Cancel the Sales Order when ERPNext allows it.
		canceled_so = _cancel_sales_order_if_safe(order, so_name)

		if canceled_so:
			msg = _("Cancelled Sales Order {0} for Medusa order {1} (Delivery Notes canceled: {2}).").format(
				so_name, order_id, len(dn_result.get("canceled") or [])
			)
		elif so.docstatus == 2:
			msg = _("Sales Order {0} already cancelled for Medusa order {1}.").format(so_name, order_id)
		else:
			msg = _(
				"Medusa order {1} cancelled: Delivery Notes canceled ({2}); Sales Order {0} kept "
				"(a submitted Sales Invoice keeps it linked — refund workflow will reverse it)."
			).format(so_name, order_id, len(dn_result.get("canceled") or []))

		create_medusa_log(
			status="Success",
			message=msg,
			request_data={"order_id": order_id},
			response_data={
				"sales_order": so_name,
				"sales_order_canceled": canceled_so,
				"delivery_notes": dn_result,
				"sales_invoice": si,
			},
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


def _get_payment_id(order: dict) -> str | None:
	"""Return the first payment ID from a Medusa order."""
	for collection in order.get("payment_collections") or []:
		for payment in collection.get("payments") or []:
			payment_id = cstr(payment.get("id"))
			if payment_id:
				return payment_id

	return None


def get_sales_order(order_id: str):
	"""Return Sales Order doc for a Medusa order id, or None."""
	name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(order_id)}, "name")
	if name:
		return frappe.get_doc("Sales Order", name)
	return None


def sync_old_orders() -> dict | None:
	"""Bulk Sync Orders job (scheduled / on save).

	Reads ``sync_old_orders``, ``old_orders_from``, ``old_orders_to`` from
	Medusa Settings. For each order in range, applies the **full lifecycle**
	(SO + payment + fulfillment + cancel/status), not create-only.

	After the run finishes, clears the ``sync_old_orders`` checkbox.
	"""
	settings = frappe.get_single(SETTING_DOCTYPE)
	if not settings.enabled or not cint(settings.get("sync_old_orders")):
		return None

	from_date = settings.get("old_orders_from")
	to_date = settings.get("old_orders_to")
	if not from_date or not to_date:
		create_medusa_log(
			status="Invalid",
			message=_("Sync Orders is enabled but From/To dates are missing."),
			method="medusa_connector.order.sync.sync_old_orders",
			make_new=True,
		)
		_clear_sync_old_orders_flag()
		return {"synced": 0, "created": 0, "failed": 0, "status": "Invalid"}

	result = _run_old_orders_sync(from_date, to_date)
	_clear_sync_old_orders_flag()
	return result


def _run_old_orders_sync(from_date, to_date, *, force: bool = False) -> dict:
	"""Core date-range loop: full lifecycle sync for every order in range.

	``force`` is retained for callers; existing Sales Orders are **not** skipped —
	lifecycle catch-up (SI/DN/cancel/status) always runs.
	"""
	from medusa_connector.medusa.order import OrderService

	from_iso = _to_iso_z(from_date)
	to_iso = _to_iso_z(to_date)

	service = OrderService()
	synced = 0
	created = 0
	failed = 0
	processed = 0

	parent_log = create_medusa_log(
		status="Queued",
		method="medusa_connector.order.sync.sync_old_orders",
		message=_("Syncing Medusa orders (full lifecycle) from {0} to {1}").format(from_iso, to_iso),
		request_data={"from": from_iso, "to": to_iso, "full_lifecycle": True},
		make_new=True,
	)
	parent_log_name = getattr(parent_log, "name", None) or cstr(parent_log)

	try:
		for order in service.iter_orders(created_at_gte=from_iso, created_at_lte=to_iso):
			order_id = cstr(order.get("id") or "")
			if not order_id:
				continue
			processed += 1
			had_so = bool(frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}))

			# Full Admin order: items, addresses, fulfillments, payment collections
			full = service.get_order(order_id) or order
			log = create_medusa_log(
				status="Queued",
				method="medusa_connector.order.sync.sync_sales_order",
				message=_("Sync order {0} (lifecycle)").format(order_id),
				request_data={
					"order_id": order_id,
					"bulk": True,
					"payment_status": full.get("payment_status"),
					"fulfillment_status": full.get("fulfillment_status"),
					"status": full.get("status"),
				},
				make_new=True,
			)
			log_name = getattr(log, "name", None) or cstr(log)
			result = sync_sales_order(full, request_id=log_name)
			if result:
				synced += 1
				if not had_so:
					created += 1
			else:
				failed += 1

		summary = {
			"synced": synced,
			"created": created,
			"updated": max(synced - created, 0),
			"failed": failed,
			"processed": processed,
			"from": from_iso,
			"to": to_iso,
			"full_lifecycle": True,
		}
		frappe.flags.request_id = parent_log_name
		create_medusa_log(
			status="Success" if not failed else "Error",
			message=_(
				"Order sync finished: {0} synced ({1} new SO, {2} updated), {3} failed ({4} in range)."
			).format(synced, created, max(synced - created, 0), failed, processed),
			response_data=summary,
			method="medusa_connector.order.sync.sync_old_orders",
		)
		return summary
	except Exception as exc:
		frappe.flags.request_id = parent_log_name
		create_medusa_log(
			status="Error",
			exception=exc,
			method="medusa_connector.order.sync.sync_old_orders",
			request_data={"from": from_iso, "to": to_iso},
		)
		return {
			"synced": synced,
			"created": created,
			"failed": failed + 1,
			"processed": processed,
		}


def _clear_sync_old_orders_flag() -> None:
	"""Turn off Sync Old Orders after a run (standard behaviour)."""
	try:
		doc = frappe.get_doc(SETTING_DOCTYPE)
		if not cint(doc.sync_old_orders):
			return
		doc.sync_old_orders = 0
		doc.flags.ignore_mandatory = True
		doc.flags.ignore_webhook_sync = True
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		# Ensure the flag is cleared even if full save fails
		frappe.db.set_single_value(SETTING_DOCTYPE, "sync_old_orders", 0)
		frappe.db.commit()


def _to_iso_z(value) -> str:
	"""Normalize datetime for Medusa Admin filters (ISO-8601)."""
	dt = get_datetime(value)
	# Medusa accepts ISO timestamps; keep naive as UTC-ish string without timezone issues.
	return dt.isoformat()


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


def _is_canceled(order: dict) -> bool:
	status = cstr(order.get("status") or "").lower()
	return status in {"canceled", "cancelled"}


def _is_refunded(order: dict) -> bool:
	status = cstr(order.get("payment_status") or "").lower()
	return status in {"refunded", "partially_refunded"}


def _primary_captured_payment_id(order: dict) -> str | None:
	"""First captured payment id from order.payment_collections (Medusa v2)."""
	for collection in order.get("payment_collections") or []:
		if not isinstance(collection, dict):
			continue
		for payment in collection.get("payments") or []:
			if not isinstance(payment, dict):
				continue
			pid = cstr(payment.get("id") or "")
			if not pid:
				continue
			# Prefer explicitly captured payments
			if payment.get("captured_at") or cstr(payment.get("status") or "").lower() == "captured":
				return pid
		# Fallback: first payment on a completed collection
		if cstr(collection.get("status") or "").lower() in {"completed", "captured"}:
			for payment in collection.get("payments") or []:
				if isinstance(payment, dict) and payment.get("id"):
					return cstr(payment.get("id"))
	return None


def _cancel_sales_order_if_safe(order: dict, sales_order_name: str) -> bool:
	"""Cancel SO when Medusa order is canceled and ERPNext permits it.

	A submitted Sales Order can be cancelled only if no submitted Sales Invoice
	keeps it linked (ERPNext blocks cancellation of an invoiced order). Delivery
	Notes are assumed already cancelled by the caller (``cancel_order``).

	Returns True if the Sales Order was cancelled, False otherwise.
	Does not write integration logs (caller / cancel_order handle logging).
	"""
	order_id = cstr(order.get("id") or "")
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		return False

	so = frappe.get_doc("Sales Order", sales_order_name)
	status_label = _status_label(order)

	# Only a submitted SI blocks SO cancellation; a draft/cancelled SI does not.
	submitted_si = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1}, "name")

	if not submitted_si and so.docstatus == 1:
		so.cancel()
		return True

	# Could not cancel — at least keep the status field consistent.
	if so.docstatus == 1:
		frappe.db.set_value(
			"Sales Order", sales_order_name, ORDER_STATUS_FIELD, status_label, update_modified=False
		)
	return False


def _status_label(order: dict) -> str:
	parts = [
		cstr(order.get("status") or ""),
		cstr(order.get("payment_status") or ""),
		cstr(order.get("fulfillment_status") or ""),
	]
	return " / ".join(p for p in parts if p)[:140]


def _lifecycle_summary(lifecycle: dict | None) -> str:
	if not lifecycle:
		return "-"
	bits = []
	if lifecycle.get("sales_invoice"):
		bits.append(f"SI={lifecycle['sales_invoice']}")
	dns = lifecycle.get("delivery_notes") or []
	if dns:
		bits.append(f"DNx{len(dns)}")
	if lifecycle.get("order_canceled"):
		bits.append("SO cancelled")
	if lifecycle.get("refund_status"):
		bits.append(f"refund={lifecycle['refund_status']}")
	if lifecycle.get("errors"):
		bits.append(f"errors={len(lifecycle['errors'])}")
	ps = lifecycle.get("payment_status")
	fs = lifecycle.get("fulfillment_status")
	if ps or fs:
		bits.append(f"state={ps or '-'}/{fs or '-'}")
	return ", ".join(bits) if bits else "status only"


def update_order_status_fields(order: dict) -> None:
	"""Refresh Medusa status custom field on linked SO / SI / DN."""
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
	for dn in frappe.get_all("Delivery Note", filters={ORDER_ID_FIELD: order_id}, pluck="name"):
		frappe.db.set_value("Delivery Note", dn, ORDER_STATUS_FIELD, label, update_modified=False)


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
