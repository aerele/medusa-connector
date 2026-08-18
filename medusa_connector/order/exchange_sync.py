# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Medusa exchange → ERPNext handling (return + new delivery)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate

from medusa_connector.constants import (
	EXCHANGE_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	RETURN_ID_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.order._shared import resolve_item_code, result
from medusa_connector.utils.logging import logged_sync


class ExchangeSync:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)

	@logged_sync("medusa_connector.order.exchange_sync.ExchangeSync.process")
	def process(
		self, *, exchange_id: str, order_id: str | None = None, request_id: str | None = None
	) -> dict[str, Any]:
		"""Process a Medusa exchange (return old items + deliver new items).

		Returns ``{status, exchange_id, order_id, return_dn, delivery_note, message}``.
		"""
		if not self.settings.enabled:
			return result("skipped", message=_("Medusa Connector is disabled"))

		exchange_id = cstr(exchange_id or "")
		if not exchange_id:
			return result("invalid", message=_("Missing exchange id"))

		# Resolve order and exchange data
		order, exchange_data = self._resolve_order_and_exchange(exchange_id, order_id)
		if not order:
			return result(
				"invalid", message=_("Could not load Medusa order for exchange {0}").format(exchange_id)
			)
		if not exchange_data:
			return result(
				"invalid",
				order_id=cstr(order.get("id")),
				exchange_id=exchange_id,
				message=_("Exchange {0} not found").format(exchange_id),
			)

		oid = cstr(order.get("id") or order_id)
		estatus = exchange_data.get("status", "requested")

		# Check for existing processing
		existing_return_dn = frappe.db.get_value(
			"Delivery Note", {"is_return": 1, EXCHANGE_ID_FIELD: exchange_id}, "name"
		)
		existing_new_dn = frappe.db.get_value(
			"Delivery Note", {"is_return": 0, EXCHANGE_ID_FIELD: exchange_id}, "name"
		)

		if existing_return_dn or existing_new_dn:
			return result(
				"skipped",
				exchange_id=exchange_id,
				order_id=oid,
				return_dn=existing_return_dn,
				delivery_note=existing_new_dn,
				message=_("Exchange {0} already processed").format(exchange_id),
			)

		# Get the Sales Order
		from medusa_connector.order.sales_order_sync import SalesOrderSync

		so = SalesOrderSync.get_sales_order_doc(oid)
		if not so:
			return result(
				"invalid",
				order_id=oid,
				exchange_id=exchange_id,
				message=_("Sales Order not found for Medusa order {0}").format(oid),
			)

		# Process return portion
		return_dn_name = None
		additional_items = exchange_data.get("additional_items", [])

		exchange_return = exchange_data.get("return") if isinstance(exchange_data.get("return"), dict) else {}
		return_id = cstr(exchange_return.get("id") or exchange_data.get("return_id") or "")
		if return_id:
			from medusa_connector.order.return_sync import ReturnSync

			return_dn_name = (
				ReturnSync(self.settings).process(return_id=return_id, order_id=oid).get("delivery_note")
			)

		# Process new items delivery (if any additional items)
		new_dn_name = None
		if additional_items:
			new_dn_name = self._create_exchange_new_delivery_note(order, exchange_data, so)

		# Update order status
		frappe.db.set_value(
			"Sales Order", so.name, ORDER_STATUS_FIELD, f"exchange_{estatus}", update_modified=False
		)

		return result(
			"success" if (return_dn_name or new_dn_name) else "skipped",
			exchange_id=exchange_id,
			order_id=oid,
			return_dn=return_dn_name,
			delivery_note=new_dn_name,
			sales_order=so.name,
			message=_("Exchange {0}: Return DN={1}, New DN={2}").format(
				exchange_id, return_dn_name or "-", new_dn_name or "-"
			),
		)

	@staticmethod
	def _resolve_order_and_exchange(
		exchange_id: str, order_id: str | None
	) -> tuple[dict | None, dict | None]:
		"""Fetch order and exchange data from Medusa."""
		from medusa_connector.medusa.order import OrderService

		oid = cstr(order_id or "")
		eid = cstr(exchange_id or "")
		order = None

		if not oid:
			entity = OrderService().get_exchange(eid)
			oid = cstr(entity.get("order_id") or (entity.get("order") or {}).get("id") or "")
		if oid:
			order = OrderService().get_order(
				oid, fields="id,display_id,status,*exchanges,*exchanges.items,*items"
			)

		if not order:
			return None, None

		# Find the exchange in the order
		exchange_data = next(
			(
				row
				for row in order.get("exchanges") or []
				if isinstance(row, dict) and cstr(row.get("id")) == eid
			),
			None,
		)

		return order, exchange_data

	def _create_exchange_return_delivery_note(
		self, order: dict, exchange_data: dict, sales_order
	) -> str | None:
		"""Create a return Delivery Note for exchanged (returned) items."""
		exchange_id = cstr(exchange_data.get("id") or "")
		order_id = cstr(order.get("id") or "")

		if not exchange_id or not order_id or not sales_order or sales_order.docstatus != 1:
			return None

		# Create Delivery Note from Sales Order
		from erpnext.selling.doctype.sales_order.mapper import make_delivery_note

		dn = make_delivery_note(sales_order.name)

		# CRITICAL: Set is_return flag for exchange return Delivery Notes
		dn.is_return = 1

		# Set identifiers
		dn.set(ORDER_ID_FIELD, order_id)
		dn.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		dn.set(EXCHANGE_ID_FIELD, exchange_id)
		dn.set(ORDER_STATUS_FIELD, "exchange_return")

		# Set posting date
		dn.set_posting_time = 1
		dn.posting_date = (
			getdate(exchange_data.get("created_at") or order.get("created_at")) or frappe.utils.nowdate()
		)

		dn.naming_series = self.settings.get("delivery_note_series") or "DN-MED-"
		warehouse = self.settings.get("warehouse") or "Stores - *"

		# Process exchange return items
		exchange_items = self._get_exchange_return_items(dn.items, exchange_data, order, warehouse)
		if not exchange_items:
			return None

		dn.items = exchange_items

		# Apply tracking if available
		if exchange_data.get("return_tracking_numbers"):
			tracking = exchange_data.get("return_tracking_numbers")
			if isinstance(tracking, list) and tracking:
				dn.lr_no = cstr(tracking[0])[:140]
			elif isinstance(tracking, str):
				dn.lr_no = tracking[:140]

		# Set cost center if configured
		if self.settings.get("cost_center"):
			for row in dn.items:
				row.cost_center = self.settings.cost_center

		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()

		return dn.name

	def _create_exchange_new_delivery_note(self, order: dict, exchange_data: dict, sales_order) -> str | None:
		"""Create a new Delivery Note for replacement items in the exchange."""
		exchange_id = cstr(exchange_data.get("id") or "")
		order_id = cstr(order.get("id") or "")

		if not exchange_id or not order_id or not sales_order or sales_order.docstatus != 1:
			return None

		additional_items = exchange_data.get("additional_items", [])
		if not additional_items:
			return None

		# Create Delivery Note from Sales Order
		dn = frappe.get_doc(
			{
				"doctype": "Delivery Note",
				"customer": sales_order.customer,
				"company": sales_order.company,
				"currency": sales_order.currency,
				"conversion_rate": sales_order.conversion_rate,
				"selling_price_list": sales_order.selling_price_list,
			}
		)

		# Set identifiers
		dn.set(ORDER_ID_FIELD, order_id)
		dn.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		dn.set(EXCHANGE_ID_FIELD, exchange_id)
		dn.set(ORDER_STATUS_FIELD, "exchange_new")

		# Set posting date
		dn.set_posting_time = 1
		dn.posting_date = (
			getdate(exchange_data.get("created_at") or order.get("created_at")) or frappe.utils.nowdate()
		)

		dn.naming_series = self.settings.get("delivery_note_series") or "DN-MED-"
		warehouse = self.settings.get("warehouse") or "Stores - *"

		# Process new items. Exchange additions are not part of the submitted original Sales Order.
		new_items = []
		for item in additional_items:
			if not isinstance(item, dict):
				continue
			item_code = resolve_item_code(item)
			qty = flt(item.get("quantity") or 0)
			if item_code and qty > 0:
				new_items.append(
					{
						"item_code": item_code,
						"qty": qty,
						"warehouse": warehouse,
						"rate": flt(item.get("unit_price") or item.get("subtotal")) / qty if qty else 0,
					}
				)
		if not new_items:
			return None

		dn.items = new_items

		# Set cost center if configured
		if self.settings.get("cost_center"):
			for row in dn.items:
				row.cost_center = self.settings.cost_center

		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()

		return dn.name

	@staticmethod
	def _get_exchange_return_items(dn_items, exchange_data: dict, order: dict, warehouse: str) -> list:
		"""Map Medusa exchange return items to ERPNext Delivery Note items."""
		resolved: list[dict] = []

		for eline in exchange_data.get("items") or []:
			if not isinstance(eline, dict):
				continue

			item_code = ExchangeSync._item_code_for_exchange_line(eline, order)
			qty = flt(eline.get("quantity") or 0)

			if item_code and qty > 0:
				resolved.append({"item_code": item_code, "qty": qty})

		# Build final items list
		final_items = []
		for dn_item in dn_items:
			match = next((r for r in resolved if r["item_code"] == dn_item.item_code), None)
			if not match:
				continue
			resolved.remove(match)
			update = {"qty": match["qty"], "warehouse": warehouse}
			final_items.append(dn_item.update(update))

		return final_items

	@staticmethod
	def _get_exchange_new_items(dn_items, additional_items: list, order: dict, warehouse: str) -> list:
		"""Map Medusa exchange new items to ERPNext Delivery Note items."""
		resolved: list[dict] = []

		for new_item in additional_items:
			if not isinstance(new_item, dict):
				continue

			item_code = ExchangeSync._item_code_for_exchange_line(new_item, order)
			qty = flt(new_item.get("quantity") or 0)

			if item_code and qty > 0:
				resolved.append({"item_code": item_code, "qty": qty})

		# Build final items list
		final_items = []
		for dn_item in dn_items:
			match = next((r for r in resolved if r["item_code"] == dn_item.item_code), None)
			if not match:
				continue
			resolved.remove(match)
			update = {"qty": match["qty"], "warehouse": warehouse}
			final_items.append(dn_item.update(update))

		return final_items

	@staticmethod
	def _item_code_for_exchange_line(eline: dict, order: dict) -> str | None:
		"""Resolve ERPNext item code for an exchange line item."""
		line_item_id = cstr(eline.get("item_id") or eline.get("line_item_id") or "")
		if not line_item_id:
			return None

		for line in order.get("items") or []:
			if isinstance(line, dict) and cstr(line.get("id")) == line_item_id:
				return resolve_item_code(line)
		return None
