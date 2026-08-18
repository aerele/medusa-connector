# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Medusa fulfillment → ERPNext Delivery Note."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import cint, cstr, flt, getdate, nowdate

from medusa_connector.constants import (
	FULFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
	MedusaOperationStatus,
)
from medusa_connector.order._shared import resolve_item_code, result
from medusa_connector.utils.logging import logged_sync


class FulfillmentSync:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)

	@logged_sync("medusa_connector.order.fulfillment.FulfillmentSync.sync")
	def sync(
		self, *, order_id: str | None = None, fulfillment_id: str | None = None, request_id: str | None = None
	) -> dict[str, Any]:
		"""Ensure a Delivery Note exists for one Medusa fulfillment."""
		if not self.settings.enabled:
			return result(MedusaOperationStatus.SKIPPED, message=_("Medusa Connector is disabled"))
		if not cint(self.settings.get("sync_delivery_note")):
			return result(
				MedusaOperationStatus.SKIPPED, message=_("Sync Delivery Note is disabled on Medusa Settings")
			)

		order, fulfillment = self._resolve_order_and_fulfillment(order_id, fulfillment_id)
		if not order:
			return result(
				MedusaOperationStatus.ERROR,
				message=_("Could not load Medusa order for fulfillment {0}").format(fulfillment_id or "-"),
			)
		if not fulfillment:
			return result(
				MedusaOperationStatus.ERROR,
				order_id=cstr(order.get("id")),
				fulfillment_id=cstr(fulfillment_id),
				message=_("Fulfillment {0} not found on order {1}").format(
					fulfillment_id or "-", order.get("id")
				),
			)

		fid = cstr(fulfillment.get("id") or fulfillment_id)
		oid = cstr(order.get("id") or order_id)

		if fulfillment.get("canceled_at"):
			# Redirect to the SAME entry point's logic without double-logging:
			# call the undecorated impl, not self.cancel().
			return self._cancel_impl(fulfillment_id=fid, order_id=oid)

		from medusa_connector.order.sales_order_sync import SalesOrderSync

		so = SalesOrderSync.get_sales_order_doc(oid)
		if not so:
			return result(
				MedusaOperationStatus.ERROR,
				order_id=oid,
				fulfillment_id=fid,
				message=_(
					"Sales Order not found for Medusa order {0}. Wait for order.placed to be "
					"processed, then retry this fulfillment event."
				).format(oid),
			)
		dn_name = self.create_delivery_note(order, fulfillment, so)
		return result(
			MedusaOperationStatus.SUCCESS,
			order_id=oid,
			fulfillment_id=fid,
			delivery_note=dn_name,
			sales_order=so.name,
			message=_("Delivery Note {0}").format(dn_name) if dn_name else _("No Delivery Note"),
		)

	@logged_sync("medusa_connector.order.fulfillment.FulfillmentSync.cancel")
	def cancel(
		self, *, fulfillment_id: str, order_id: str | None = None, request_id: str | None = None
	) -> dict[str, Any]:
		"""Cancel the Delivery Note for a canceled Medusa fulfillment."""
		return self._cancel_impl(fulfillment_id=fulfillment_id, order_id=order_id)

	def sync_for_order(self, order: dict, sales_order) -> dict[str, list[str]]:
		"""Apply all fulfillments on an order — used by OrderSync's lifecycle
		so historical orders catch up without needing individual webhooks."""
		out: dict[str, list[str]] = {"delivery_notes": [], "canceled": []}
		if not cint(self.settings.get("sync_delivery_note")) or not sales_order:
			return out
		order_id = cstr(order.get("id") or "")
		for fulfillment in order.get("fulfillments") or []:
			if not isinstance(fulfillment, dict) or not fulfillment.get("id"):
				continue
			fid = cstr(fulfillment.get("id"))
			if fulfillment.get("canceled_at"):
				self.cancel(fulfillment_id=fid, order_id=order_id)
				out["canceled"].append(fid)
				continue
			name = self.create_delivery_note(order, fulfillment, sales_order)
			if name:
				out["delivery_notes"].append(name)
		return out

	def create_delivery_note(self, order: dict, fulfillment: dict, sales_order) -> str | None:
		"""Create or update a Delivery Note for one fulfillment. Returns DN name."""
		if not cint(self.settings.get("sync_delivery_note")):
			return None
		fulfillment_id = cstr(fulfillment.get("id") or "")
		order_id = cstr(order.get("id") or "")
		if not fulfillment_id or not order_id or not sales_order or sales_order.docstatus != 1:
			return None
		existing = frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, "name")
		if existing:
			self._update_existing_delivery_note(existing, order, fulfillment)
			return existing
		dn = make_delivery_note(sales_order.name)
		dn.set(ORDER_ID_FIELD, order_id)
		dn.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		dn.set(FULFILLMENT_ID_FIELD, fulfillment_id)
		dn.set(ORDER_STATUS_FIELD, self._fulfillment_status_label(order, fulfillment))
		dn.set_posting_time = 1
		dn.posting_date = (
			getdate(fulfillment.get("shipped_at") or fulfillment.get("created_at") or order.get("created_at"))
			or nowdate()
		)
		dn.naming_series = self.settings.get("delivery_note_series") or "DN-MED-"
		warehouse = self._warehouse_for_fulfillment(fulfillment)
		dn.items = self._get_fulfillment_items(dn.items, fulfillment, order, warehouse)
		if not dn.items:
			frappe.logger("medusa_connector").warning(
				f"No mappable items for Medusa fulfillment {fulfillment_id} on order {order_id}; DN not created"
			)
			return None
		self._apply_tracking(dn, fulfillment)
		if self.settings.get("cost_center"):
			for row in dn.items:
				row.cost_center = self.settings.cost_center
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()
		return dn.name

	def cancel_order_delivery_notes(
		self, order_id: str, *, status_label: str | None = None
	) -> dict[str, Any]:
		"""Cancel every Delivery Note linked to a Medusa order."""
		order_id = cstr(order_id)
		out: dict[str, Any] = {"canceled": [], "skipped": [], "failed": [], "order_id": order_id}
		if not order_id:
			return out
		if status_label:
			dn = DocType("Delivery Note")
			(
				frappe.qb.update(dn)
				.set(dn[ORDER_STATUS_FIELD], status_label)
				.where(dn[ORDER_ID_FIELD] == order_id)
			).run()
		for row in frappe.get_all(
			"Delivery Note", filters={ORDER_ID_FIELD: order_id}, fields=["name", "docstatus"]
		):
			if row.docstatus == 1:
				frappe.get_doc("Delivery Note", row.name).cancel()
				out["canceled"].append(row.name)
			else:
				out["skipped"].append(row.name)
		return out

	# -- internals -------------------------------------------------------
	def _cancel_impl(self, *, fulfillment_id: str, order_id: str | None = None) -> dict[str, Any]:
		"""Business logic only — no logging. Called by both ``cancel()`` and
		``sync()``'s cancel-redirect so there's exactly one log write."""
		fulfillment_id = cstr(fulfillment_id)
		if not fulfillment_id:
			return result(MedusaOperationStatus.ERROR, message=_("Missing fulfillment_id"))
		dn_name = frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, "name")
		if not dn_name:
			return result(
				MedusaOperationStatus.ERROR,
				fulfillment_id=fulfillment_id,
				order_id=cstr(order_id),
				message=_("No Delivery Note for fulfillment {0}").format(fulfillment_id),
			)
		dn = frappe.get_doc("Delivery Note", dn_name)
		if order_id:
			frappe.db.set_value(
				"Delivery Note", dn_name, ORDER_STATUS_FIELD, "canceled", update_modified=False
			)
		if dn.docstatus == 1:
			dn.cancel()
			msg = _("Cancelled Delivery Note {0} for fulfillment {1}").format(dn_name, fulfillment_id)
		else:
			msg = _("Delivery Note {0} already cancelled").format(dn_name)
		return result(
			MedusaOperationStatus.SUCCESS,
			fulfillment_id=fulfillment_id,
			order_id=cstr(order_id),
			delivery_note=dn_name,
			message=msg,
		)

	@staticmethod
	def _resolve_order_and_fulfillment(
		order_id: str | None, fulfillment_id: str | None
	) -> tuple[dict | None, dict | None]:
		from medusa_connector.constants import DEFAULT_ORDER_FULFILLMENT_FIELDS
		from medusa_connector.medusa.order import OrderService

		oid = cstr(order_id or "")
		fid = cstr(fulfillment_id or "")
		if not oid:
			return None, None
		order = OrderService().get_order(oid, fields=DEFAULT_ORDER_FULFILLMENT_FIELDS)
		if not order:
			return None, None
		fulfillment = next(
			(
				row
				for row in order.get("fulfillments") or []
				if isinstance(row, dict) and cstr(row.get("id")) == fid
			),
			None,
		)
		return order, fulfillment

	@staticmethod
	def _get_fulfillment_items(dn_items, fulfillment: dict, order: dict, warehouse: str | None) -> list:
		resolved: list[dict] = []
		for fline in deepcopy(fulfillment.get("items") or []):
			if not isinstance(fline, dict):
				continue
			item_code = FulfillmentSync._item_code_for_fulfillment_line(fline, order)
			qty = flt(fline.get("quantity") or 0)
			if item_code and qty > 0:
				resolved.append({"item_code": item_code, "qty": qty})
		final_items = []
		for dn_item in dn_items:
			match = next((r for r in resolved if r["item_code"] == dn_item.item_code), None)
			if not match:
				continue
			resolved.remove(match)
			update = {"qty": match["qty"]}
			if warehouse:
				update["warehouse"] = warehouse
			final_items.append(dn_item.update(update))
		return final_items

	@staticmethod
	def _item_code_for_fulfillment_line(fline: dict, order: dict) -> str | None:
		line_item_id = cstr(fline.get("line_item_id") or "")
		if not line_item_id:
			return None
		for line in order.get("items") or []:
			if isinstance(line, dict) and cstr(line.get("id")) == line_item_id:
				return resolve_item_code(line)
		return None

	def _warehouse_for_fulfillment(self, fulfillment: dict) -> str | None:
		location_id = cstr(fulfillment.get("location_id") or "")
		if location_id and hasattr(self.settings, "get_medusa_to_erpnext_wh_mapping"):
			mapped = (self.settings.get_medusa_to_erpnext_wh_mapping() or {}).get(location_id)
			if mapped:
				return mapped
		return self.settings.get("warehouse")

	@staticmethod
	def _apply_tracking(dn, fulfillment: dict) -> None:
		tracking_numbers = [
			cstr(label.get("tracking_number"))
			for label in fulfillment.get("labels") or []
			if isinstance(label, dict) and label.get("tracking_number")
		]
		if tracking_numbers and hasattr(dn, "lr_no"):
			dn.lr_no = tracking_numbers[0][:140]

	@staticmethod
	def _update_existing_delivery_note(dn_name: str, order: dict, fulfillment: dict) -> None:
		updates = {ORDER_STATUS_FIELD: FulfillmentSync._fulfillment_status_label(order, fulfillment)}
		for label in fulfillment.get("labels") or []:
			if (
				isinstance(label, dict)
				and label.get("tracking_number")
				and frappe.get_meta("Delivery Note").has_field("lr_no")
			):
				updates["lr_no"] = cstr(label.get("tracking_number"))[:140]
				break
		frappe.db.set_value("Delivery Note", dn_name, updates, update_modified=False)

	@staticmethod
	def _fulfillment_status_label(order: dict, fulfillment: dict) -> str:
		if fulfillment.get("canceled_at"):
			state = "canceled"
		elif fulfillment.get("delivered_at"):
			state = "delivered"
		elif fulfillment.get("shipped_at"):
			state = "shipped"
		elif fulfillment.get("packed_at"):
			state = "packed"
		else:
			state = "fulfilled"
		order_fs = cstr(order.get("fulfillment_status") or "")
		return (f"{state} / {order_fs}" if order_fs else state)[:140]
