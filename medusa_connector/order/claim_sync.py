# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt
"""Medusa claim → ERPNext handling (refund or replacement)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate

from medusa_connector.constants import (
	CLAIM_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.order._shared import resolve_item_code, result
from medusa_connector.order.refund import RefundSync
from medusa_connector.order.return_sync import ReturnSync
from medusa_connector.utils.logging import logged_sync


class ClaimSync:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTING_DOCTYPE)

	@logged_sync("medusa_connector.order.claim_sync.ClaimSync.process")
	def process(
		self, *, claim_id: str, order_id: str | None = None, request_id: str | None = None
	) -> dict[str, Any]:
		"""Process a Medusa claim (refund or replacement).

		Returns ``{status, claim_id, order_id, delivery_note, sales_invoice, payment_entry, message}``.
		"""
		if not self.settings.enabled:
			return result("skipped", message=_("Medusa Connector is disabled"))

		claim_id = cstr(claim_id or "")
		if not claim_id:
			return result("invalid", message=_("Missing claim id"))

		# Resolve order and claim data
		order, claim_data = self._resolve_order_and_claim(claim_id, order_id)
		if not order:
			return result("invalid", message=_("Could not load Medusa order for claim {0}").format(claim_id))
		if not claim_data:
			return result(
				"invalid",
				order_id=cstr(order.get("id")),
				claim_id=claim_id,
				message=_("Claim {0} not found").format(claim_id),
			)

		oid = cstr(order.get("id") or order_id)
		claim_type = claim_data.get("type", "refund")

		# Check for existing processing
		existing_si = frappe.db.get_value("Sales Invoice", {CLAIM_ID_FIELD: claim_id}, "name")
		existing_dn = frappe.db.get_value("Delivery Note", {CLAIM_ID_FIELD: claim_id}, "name")

		if existing_si or existing_dn:
			return result(
				"skipped",
				claim_id=claim_id,
				order_id=oid,
				sales_invoice=existing_si,
				delivery_note=existing_dn,
				message=_("Claim {0} already processed").format(claim_id),
			)

		# Get the Sales Order
		from medusa_connector.order.sales_order_sync import SalesOrderSync

		so = SalesOrderSync.get_sales_order_doc(oid)
		if not so:
			return result(
				"invalid",
				order_id=oid,
				claim_id=claim_id,
				message=_("Sales Order not found for Medusa order {0}").format(oid),
			)

		# Process based on claim type
		if claim_type == "replace":
			# For replacement claims, create return DN
			claim_return = claim_data.get("return") if isinstance(claim_data.get("return"), dict) else {}
			return_id = cstr(claim_return.get("id") or claim_data.get("return_id") or "")
			return_result = (
				ReturnSync(self.settings).process(return_id=return_id, order_id=oid) if return_id else {}
			)
			dn_name = return_result.get("delivery_note")
			from medusa_connector.order.exchange_sync import ExchangeSync

			replacement_dn = ExchangeSync(self.settings)._create_exchange_new_delivery_note(
				order,
				{
					"id": claim_id,
					"created_at": claim_data.get("created_at"),
					"additional_items": claim_data.get("additional_items") or [],
				},
				so,
			)
			if replacement_dn and frappe.get_meta("Delivery Note").has_field(CLAIM_ID_FIELD):
				frappe.db.set_value(
					"Delivery Note", replacement_dn, CLAIM_ID_FIELD, claim_id, update_modified=False
				)
			result_data = result(
				"success" if dn_name else "skipped",
				claim_id=claim_id,
				order_id=oid,
				delivery_note=dn_name,
				replacement_delivery_note=replacement_dn,
				sales_order=so.name,
				message=_("Claim {0}: Replacement return processed as DN {1}").format(
					claim_id, dn_name or "-"
				),
			)
		elif claim_type == "refund":
			# For refund claims, reverse invoice and payment
			reversal = self._reverse_claim_payment(order, claim_data, so)
			result_data = result(
				reversal.get("status", "skipped"),
				claim_id=claim_id,
				order_id=oid,
				sales_invoice=reversal.get("sales_invoice"),
				payment_entry=reversal.get("payment_entry"),
				sales_order=so.name,
				message=reversal.get("message", _("Claim {0}: Refund processed").format(claim_id)),
			)
		else:
			# Unknown claim type - skip
			result_data = result(
				"skipped",
				claim_id=claim_id,
				order_id=oid,
				message=_("Claim {0}: Unknown claim type '{1}'").format(claim_id, claim_type),
			)

		# Update order status
		frappe.db.set_value(
			"Sales Order", so.name, ORDER_STATUS_FIELD, f"claim_{claim_type}", update_modified=False
		)

		return result_data

	@staticmethod
	def _resolve_order_and_claim(claim_id: str, order_id: str | None) -> tuple[dict | None, dict | None]:
		"""Fetch order and claim data from Medusa."""
		from medusa_connector.medusa.order import OrderService

		oid = cstr(order_id or "")
		cid = cstr(claim_id or "")
		order = None

		if not oid:
			entity = OrderService().get_claim(cid)
			oid = cstr(entity.get("order_id") or (entity.get("order") or {}).get("id") or "")
		if oid:
			order = OrderService().get_order(oid, fields="id,display_id,status,*claims,*claims.items,*items")

		if not order:
			return None, None

		# Find the claim in the order
		claim_data = next(
			(
				row
				for row in order.get("claims") or []
				if isinstance(row, dict) and cstr(row.get("id")) == cid
			),
			None,
		)

		return order, claim_data

	def _create_claim_return_delivery_note(self, order: dict, claim_data: dict, sales_order) -> str | None:
		"""Create a return Delivery Note for replacement claims."""
		claim_id = cstr(claim_data.get("id") or "")
		order_id = cstr(order.get("id") or "")

		if not claim_id or not order_id or not sales_order or sales_order.docstatus != 1:
			return None

		# Create Delivery Note from Sales Order
		from erpnext.selling.doctype.sales_order.mapper import make_delivery_note

		dn = make_delivery_note(sales_order.name)

		# CRITICAL: Set is_return flag for claim replacement return Delivery Notes
		dn.is_return = 1

		# Set identifiers
		dn.set(ORDER_ID_FIELD, order_id)
		dn.set(
			ORDER_NUMBER_FIELD, cstr(order.get("display_id") or order.get("custom_display_id") or order_id)
		)
		dn.set(CLAIM_ID_FIELD, claim_id)
		dn.set(ORDER_STATUS_FIELD, "claim_replacement")

		# Set posting date
		dn.set_posting_time = 1
		dn.posting_date = (
			getdate(claim_data.get("created_at") or order.get("created_at")) or frappe.utils.nowdate()
		)

		dn.naming_series = self.settings.get("delivery_note_series") or "DN-MED-"
		warehouse = self.settings.get("warehouse") or "Stores - *"

		# Process claim items
		claim_items = self._get_claim_items(dn.items, claim_data, order, warehouse)
		if not claim_items:
			return None

		dn.items = claim_items

		if self.settings.get("cost_center"):
			for row in dn.items:
				row.cost_center = self.settings.cost_center

		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()

		return dn.name

	def _reverse_claim_payment(self, order: dict, claim_data: dict, sales_order) -> dict:
		claim_id = cstr(claim_data.get("id") or "")
		order_id = cstr(order.get("id") or "")
		reversal = RefundSync(self.settings)._reverse_order_payment(
			order_id, claim_id, flt(claim_data.get("refund_amount") or 0)
		)
		for doctype, name in (
			("Sales Invoice", reversal.get("sales_invoice")),
			("Payment Entry", reversal.get("payment_entry")),
		):
			if name and frappe.get_meta(doctype).has_field(CLAIM_ID_FIELD):
				frappe.db.set_value(doctype, name, CLAIM_ID_FIELD, claim_id, update_modified=False)
		return reversal

	@staticmethod
	def _find_payment_entry(order_id: str, sales_invoice_name: str) -> str | None:
		"""Find payment entry for the order."""
		pe = frappe.db.get_value(
			"Payment Entry", {ORDER_ID_FIELD: order_id, "docstatus": 1, "payment_type": "Receive"}, "name"
		)
		if pe:
			return pe
		pe = frappe.db.get_value(
			"Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": sales_invoice_name},
			"parent",
		)
		if pe and frappe.db.get_value("Payment Entry", pe, "docstatus") == 1:
			return pe
		return None

	@staticmethod
	def _get_claim_items(dn_items, claim_data: dict, order: dict, warehouse: str) -> list:
		"""Map Medusa claim items to ERPNext Delivery Note items."""
		resolved: list[dict] = []

		for cline in claim_data.get("items") or []:
			if not isinstance(cline, dict):
				continue

			item_code = ClaimSync._item_code_for_claim_line(cline, order)
			qty = flt(cline.get("quantity") or 0)

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
	def _item_code_for_claim_line(cline: dict, order: dict) -> str | None:
		"""Resolve ERPNext item code for a claim line item."""
		line_item_id = cstr(cline.get("item_id") or cline.get("line_item_id") or "")
		if not line_item_id:
			return None

		for line in order.get("items") or []:
			if isinstance(line, dict) and cstr(line.get("id")) == line_item_id:
				return resolve_item_code(line)
		return None
