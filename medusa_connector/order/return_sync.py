# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt
"""Medusa return → ERPNext Delivery Note (return) & Credit Note."""

from __future__ import annotations

from typing import Any

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
from frappe import _
from frappe.utils import cstr, flt, getdate

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	RETURN_ID_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.order._shared import resolve_item_code, result
from medusa_connector.utils.logging import logged_sync


class ReturnSync:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)

	@logged_sync("medusa_connector.order.return_sync.ReturnSync.process")
	def process(
		self, *, return_id: str, order_id: str | None = None, request_id: str | None = None
	) -> dict[str, Any]:
		"""Process a Medusa return request/create ERPNext return Delivery Note.

		Returns ``{status, return_id, order_id, delivery_note, message}``.
		"""
		if not self.settings.enabled:
			return result("skipped", message=_("Medusa Connector is disabled"))

		return_id = cstr(return_id or "")
		if not return_id:
			return result("invalid", message=_("Missing return id"))

		# Resolve order and return data from Medusa
		order, return_data = self._resolve_order_and_return(return_id, order_id)
		if not order:
			return result(
				"invalid", message=_("Could not load Medusa order for return {0}").format(return_id)
			)
		if not return_data:
			return result(
				"invalid",
				order_id=cstr(order.get("id")),
				return_id=return_id,
				message=_("Return {0} not found").format(return_id),
			)

		oid = cstr(order.get("id") or order_id)
		rstatus = return_data.get("status", "requested")

		# Check for existing return DN
		existing_dn = frappe.db.get_value("Delivery Note", {RETURN_ID_FIELD: return_id}, "name")
		if existing_dn:
			return result(
				"skipped",
				return_id=return_id,
				order_id=oid,
				delivery_note=existing_dn,
				message=_("Return {0} already processed as Delivery Note {1}").format(return_id, existing_dn),
			)

		# Get the Sales Order
		from medusa_connector.order.sales_order_sync import SalesOrderSync

		so = SalesOrderSync.get_sales_order_doc(oid)
		if not so:
			return result(
				"invalid",
				order_id=oid,
				return_id=return_id,
				message=_(
					"Sales Order not found for Medusa order {0}. "
					"Wait for order.placed to be processed, then retry this return event."
				).format(oid),
			)

		# Create return Delivery Note
		dn_name = self._create_return_delivery_note(order, return_data, so)
		if not dn_name:
			return result(
				"skipped",
				return_id=return_id,
				order_id=oid,
				message=_("No items to return for return {0}").format(return_id),
			)

		# Update order status
		frappe.db.set_value(
			"Sales Order", so.name, ORDER_STATUS_FIELD, f"return_{rstatus}", update_modified=False
		)

		return result(
			"success",
			return_id=return_id,
			order_id=oid,
			delivery_note=dn_name,
			sales_order=so.name,
			message=_("Return {0}: Delivery Note {1} created").format(return_id, dn_name),
		)

	@staticmethod
	def _resolve_order_and_return(return_id: str, order_id: str | None) -> tuple[dict | None, dict | None]:
		"""Fetch order and return data from Medusa."""
		from medusa_connector.medusa.order import OrderService

		oid = cstr(order_id or "")
		rid = cstr(return_id or "")

		if not oid:
			# Try to get order from return endpoint
			order = OrderService().get_order(
				oid, fields="id,display_id,status,*returns,*returns.items,*items"
			)
			if order:
				oid = cstr(order.get("id") or "")
		else:
			order = OrderService().get_order(
				oid, fields="id,display_id,status,*returns,*returns.items,*items"
			)

		if not order:
			return None, None

		# Find the return in the order
		return_data = next(
			(
				row
				for row in order.get("returns") or []
				if isinstance(row, dict) and cstr(row.get("id")) == rid
			),
			None,
		)

		return order, return_data

	def _create_return_delivery_note(self, order: dict, return_data: dict, sales_order) -> str | None:
		"""Create a return Delivery Note for the returned items."""
		return_id = cstr(return_data.get("id") or "")
		order_id = cstr(order.get("id") or "")

		if not return_id or not order_id or not sales_order or sales_order.docstatus != 1:
			return None

		# Create Delivery Note from Sales Order
		dn = make_delivery_note(sales_order.name)

		# Set Medusa identifiers
		dn.set(ORDER_ID_FIELD, order_id)
		dn.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		dn.set(RETURN_ID_FIELD, return_id)
		dn.set(
			ORDER_STATUS_FIELD,
			"return_received" if return_data.get("status") == "received" else "return_requested",
		)

		# Set posting date to return request date
		dn.set_posting_time = 1
		dn.posting_date = (
			getdate(
				return_data.get("received_at") or return_data.get("requested_at") or order.get("created_at")
			)
			or frappe.utils.nowdate()
		)

		# Set naming series
		dn.naming_series = self.settings.get("delivery_note_series") or "DN-MED-"

		# Get warehouse for returns
		warehouse = self.settings.get("warehouse") or "Stores - *"

		# Process return items
		return_items = self._get_return_items(dn.items, return_data, order, warehouse)
		if not return_items:
			return None

		dn.items = return_items

		# Apply tracking if available (for return shipping)
		if return_data.get("tracking_numbers"):
			tracking = return_data.get("tracking_numbers")
			if isinstance(tracking, list) and tracking:
				dn.lr_no = cstr(tracking[0])[:140]
			elif isinstance(tracking, str):
				dn.lr_no = tracking[:140]

		# Set cost center if configured
		if self.settings.get("cost_center"):
			for row in dn.items:
				row.cost_center = self.settings.cost_center

		# Save and submit
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()

		return dn.name

	@staticmethod
	def _get_return_items(dn_items, return_data: dict, order: dict, warehouse: str) -> list:
		"""Map Medusa return items to ERPNext Delivery Note items."""
		resolved: list[dict] = []

		for rline in return_data.get("items") or []:
			if not isinstance(rline, dict):
				continue

			# Get item code from line item reference
			item_code = ReturnSync._item_code_for_return_line(rline, order)
			qty = flt(rline.get("quantity") or 0)

			if item_code and qty > 0:
				resolved.append({"item_code": item_code, "qty": qty})

		# Build final items list matching DN structure
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
	def _item_code_for_return_line(rline: dict, order: dict) -> str | None:
		"""Resolve ERPNext item code for a return line item."""
		line_item_id = cstr(rline.get("item_id") or rline.get("line_item_id") or "")
		if not line_item_id:
			return None

		for line in order.get("items") or []:
			if isinstance(line, dict) and cstr(line.get("id")) == line_item_id:
				return resolve_item_code(line)
		return None
