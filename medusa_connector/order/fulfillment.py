# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa fulfillment → ERPNext Delivery Note (Shopify-style lifecycle).

Design (aligned with ``ecommerce_integrations.shopify.fulfillment``):

* Sales Order is the hub; each Medusa fulfillment becomes **one** Delivery Note.
* Built via ``make_delivery_note(so)`` then filtered to this fulfillment's items/qty.
* Idempotent on ``medusa_fulfillment_id`` (never two DNs for the same fulfillment).
* Existing DNs are updated (tracking / status fields) instead of recreated.
* Gated by Medusa Settings ``sync_delivery_note``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from medusa_connector.constants import (
	FULFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from medusa_connector.order.sync import get_item_code, get_sales_order
from medusa_connector.utils.logging import create_medusa_log


def sync_fulfillment(
	*,
	order_id: str | None = None,
	fulfillment_id: str | None = None,
	order: dict | None = None,
	fulfillment: dict | None = None,
	request_id: str | None = None,
) -> dict[str, Any]:
	"""Ensure a Delivery Note exists for one Medusa fulfillment.

	Returns a result dict: ``{status, delivery_note, message, ...}``.
	Safe for webhooks and retries (idempotent on fulfillment id).
	"""
	frappe.set_user("Administrator")
	if request_id:
		frappe.flags.request_id = request_id

	settings = frappe.get_doc(SETTING_DOCTYPE)
	if not settings.enabled:
		return _result("skipped", message=_("Medusa Connector is disabled"))

	if not cint(settings.get("sync_delivery_note")):
		return _result("skipped", message=_("Sync Delivery Note is disabled on Medusa Settings"))

	try:
		order, fulfillment = _resolve_order_and_fulfillment(
			order_id=order_id,
			fulfillment_id=fulfillment_id,
			order=order,
			fulfillment=fulfillment,
		)
		if not order:
			return _result(
				"invalid",
				message=_("Could not load Medusa order for fulfillment {0}").format(fulfillment_id or "-"),
			)
		if not fulfillment:
			return _result(
				"invalid",
				order_id=cstr(order.get("id")),
				fulfillment_id=cstr(fulfillment_id),
				message=_("Fulfillment {0} not found on order {1}").format(
					fulfillment_id or "-", order.get("id")
				),
			)

		fid = cstr(fulfillment.get("id") or fulfillment_id)
		oid = cstr(order.get("id") or order_id)

		if fulfillment.get("canceled_at"):
			return cancel_fulfillment_delivery_note(fulfillment_id=fid, order_id=oid, request_id=request_id)

		so = get_sales_order(oid)
		if not so:
			# Ensure SO first (fulfillment can race ahead of order.placed processing)
			from medusa_connector.order.sync import sync_sales_order

			so_name = sync_sales_order(order, request_id=request_id)
			so = frappe.get_doc("Sales Order", so_name) if so_name else None

		if not so:
			return _result(
				"invalid",
				order_id=oid,
				fulfillment_id=fid,
				message=_("Sales Order not found for Medusa order {0}").format(oid),
			)

		dn_name = create_delivery_note(order, fulfillment, settings, so)
		create_medusa_log(
			status="Success",
			message=_("Delivery Note {0} for fulfillment {1} (order {2})").format(dn_name or "-", fid, oid),
			request_data={"order_id": oid, "fulfillment_id": fid},
			response_data={"delivery_note": dn_name, "sales_order": so.name},
			method="medusa_connector.order.fulfillment.sync_fulfillment",
		)
		return _result(
			"success",
			order_id=oid,
			fulfillment_id=fid,
			delivery_note=dn_name,
			sales_order=so.name,
			message=_("Delivery Note {0}").format(dn_name) if dn_name else _("No Delivery Note"),
		)
	except Exception as exc:
		create_medusa_log(
			status="Error",
			exception=exc,
			rollback=True,
			request_data={
				"order_id": order_id,
				"fulfillment_id": fulfillment_id,
			},
			method="medusa_connector.order.fulfillment.sync_fulfillment",
		)
		return _result(
			"error",
			order_id=cstr(order_id),
			fulfillment_id=cstr(fulfillment_id),
			message=str(exc),
		)


def create_delivery_notes_for_order(order: dict, settings, sales_order) -> list[str]:
	"""Create DNs for every active fulfillment on a Medusa order.

	Mirrors Shopify ``create_delivery_note`` looping ``order["fulfillments"]``.
	Canceled fulfillments are handled by :func:`sync_fulfillments_for_order`.
	"""
	return sync_fulfillments_for_order(order, settings, sales_order).get("delivery_notes") or []


def sync_fulfillments_for_order(order: dict, settings, sales_order) -> dict[str, list[str]]:
	"""Apply all fulfillments on an order: create/update DNs, cancel canceled ones.

	Used by full order lifecycle sync so historical orders catch up without webhooks.
	"""
	result: dict[str, list[str]] = {"delivery_notes": [], "canceled": []}
	if not cint(settings.get("sync_delivery_note")) or not sales_order:
		return result

	order_id = cstr(order.get("id") or "")
	for fulfillment in order.get("fulfillments") or []:
		if not isinstance(fulfillment, dict) or not fulfillment.get("id"):
			continue
		fid = cstr(fulfillment.get("id"))
		if fulfillment.get("canceled_at"):
			cancel_fulfillment_delivery_note(fulfillment_id=fid, order_id=order_id)
			result["canceled"].append(fid)
			continue
		name = create_delivery_note(order, fulfillment, settings, sales_order)
		if name:
			result["delivery_notes"].append(name)
	return result


def create_delivery_note(order: dict, fulfillment: dict, settings, sales_order) -> str | None:
	"""Create or update a Delivery Note for one fulfillment. Returns DN name."""
	if not cint(settings.get("sync_delivery_note")):
		return None

	fulfillment_id = cstr(fulfillment.get("id") or "")
	order_id = cstr(order.get("id") or "")
	if not fulfillment_id or not order_id or not sales_order:
		return None

	if sales_order.docstatus != 1:
		frappe.logger("medusa_connector").warning(
			f"Cannot create DN for fulfillment {fulfillment_id}: Sales Order {sales_order.name} not submitted"
		)
		return None

	existing = frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, "name")
	if existing:
		_update_existing_delivery_note(existing, order, fulfillment)
		return existing

	dn = make_delivery_note(sales_order.name)
	dn.set(ORDER_ID_FIELD, order_id)
	dn.set(
		ORDER_NUMBER_FIELD,
		cstr(order.get("display_id") or order.get("custom_display_id") or order_id),
	)
	dn.set(FULFILLMENT_ID_FIELD, fulfillment_id)
	dn.set(ORDER_STATUS_FIELD, _fulfillment_status_label(order, fulfillment))

	dn.set_posting_time = 1
	dn.posting_date = (
		getdate(fulfillment.get("shipped_at") or fulfillment.get("created_at") or order.get("created_at"))
		or nowdate()
	)
	dn.naming_series = settings.get("delivery_note_series") or "DN-MED-"

	warehouse = _warehouse_for_fulfillment(fulfillment, settings)
	dn.items = _get_fulfillment_items(dn.items, fulfillment, order, warehouse)

	if not dn.items:
		frappe.logger("medusa_connector").warning(
			f"No mappable items for Medusa fulfillment {fulfillment_id} on order {order_id}; DN not created"
		)
		return None

	_apply_tracking(dn, fulfillment)

	if settings.get("cost_center"):
		for row in dn.items:
			row.cost_center = settings.cost_center

	dn.flags.ignore_mandatory = True
	dn.save(ignore_permissions=True)
	dn.submit()

	return dn.name


def cancel_fulfillment_delivery_note(
	*,
	fulfillment_id: str,
	order_id: str | None = None,
	request_id: str | None = None,
) -> dict[str, Any]:
	"""Cancel the Delivery Note for a canceled Medusa fulfillment when safe."""
	frappe.set_user("Administrator")
	if request_id:
		frappe.flags.request_id = request_id

	fulfillment_id = cstr(fulfillment_id)
	if not fulfillment_id:
		return _result("invalid", message=_("Missing fulfillment_id"))

	try:
		dn_name = frappe.db.get_value("Delivery Note", {FULFILLMENT_ID_FIELD: fulfillment_id}, "name")
		if not dn_name:
			return _result(
				"invalid",
				fulfillment_id=fulfillment_id,
				order_id=cstr(order_id),
				message=_("No Delivery Note for fulfillment {0}").format(fulfillment_id),
			)

		dn = frappe.get_doc("Delivery Note", dn_name)
		status_label = "canceled"
		if order_id:
			frappe.db.set_value(
				"Delivery Note", dn_name, ORDER_STATUS_FIELD, status_label, update_modified=False
			)

		if dn.docstatus == 1:
			dn.cancel()
			msg = _("Cancelled Delivery Note {0} for fulfillment {1}").format(dn_name, fulfillment_id)
		elif dn.docstatus == 0:
			dn.delete(ignore_permissions=True)
			msg = _("Deleted draft Delivery Note {0} for fulfillment {1}").format(dn_name, fulfillment_id)
		else:
			msg = _("Delivery Note {0} already cancelled").format(dn_name)

		create_medusa_log(
			status="Success",
			message=msg,
			request_data={"fulfillment_id": fulfillment_id, "order_id": order_id},
			response_data={"delivery_note": dn_name},
			method="medusa_connector.order.fulfillment.cancel_fulfillment_delivery_note",
		)
		return _result(
			"success",
			fulfillment_id=fulfillment_id,
			order_id=cstr(order_id),
			delivery_note=dn_name,
			message=msg,
		)
	except Exception as exc:
		create_medusa_log(
			status="Error",
			exception=exc,
			rollback=True,
			request_data={"fulfillment_id": fulfillment_id, "order_id": order_id},
			method="medusa_connector.order.fulfillment.cancel_fulfillment_delivery_note",
		)
		return _result(
			"error",
			fulfillment_id=fulfillment_id,
			order_id=cstr(order_id),
			message=str(exc),
		)


def cancel_order_delivery_notes(
	order_id: str, *, status_label: str | None = None, request_id: str | None = None
) -> dict[str, Any]:
	"""Cancel every Delivery Note linked to a Medusa order.

	Used by the order-cancellation flow so that inventory booked out by
	fulfilled shipments is restored via ERPNext's standard DN cancellation
	(a cancelled DN reverses its stock ledger entry). Best-effort: a DN that
	cannot be cancelled (e.g. linked to a submitted stock reconciliation) is
	reported in ``failed`` rather than aborting the whole cancellation.

	Returns ``{canceled, deleted, skipped, failed, order_id}``.
	"""
	result: dict[str, Any] = {
		"canceled": [],
		"deleted": [],
		"skipped": [],
		"failed": [],
		"order_id": cstr(order_id),
	}
	order_id = cstr(order_id)
	if not order_id:
		return result

	dn_names = frappe.get_all("Delivery Note", filters={ORDER_ID_FIELD: order_id}, pluck="name")
	for dn_name in dn_names:
		try:
			dn = frappe.get_doc("Delivery Note", dn_name)
			if status_label:
				frappe.db.set_value(
					"Delivery Note", dn_name, ORDER_STATUS_FIELD, status_label, update_modified=False
				)
			if dn.docstatus == 1:
				dn.cancel()
				result["canceled"].append(dn_name)
			elif dn.docstatus == 0:
				dn.delete(ignore_permissions=True)
				result["deleted"].append(dn_name)
			else:
				result["skipped"].append(dn_name)
		except Exception as exc:
			result["failed"].append(dn_name)
			frappe.logger("medusa_connector").warning(
				f"Could not cancel Delivery Note {dn_name} for order {order_id}: {exc}"
			)
	return result


def update_fulfillment_tracking(
	*,
	fulfillment_id: str,
	order_id: str | None = None,
	order: dict | None = None,
	fulfillment: dict | None = None,
	request_id: str | None = None,
) -> dict[str, Any]:
	"""Update tracking on an existing DN, or create DN if missing (shipment event)."""
	# Prefer ensure/create path — creates if missing, updates tracking if present.
	return sync_fulfillment(
		order_id=order_id,
		fulfillment_id=fulfillment_id,
		order=order,
		fulfillment=fulfillment,
		request_id=request_id,
	)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_order_and_fulfillment(
	*,
	order_id: str | None,
	fulfillment_id: str | None,
	order: dict | None,
	fulfillment: dict | None,
) -> tuple[dict | None, dict | None]:
	from medusa_connector.constants import DEFAULT_ORDER_FULFILLMENT_FIELDS
	from medusa_connector.medusa.order import OrderService

	order = order if isinstance(order, dict) else {}
	fulfillment = fulfillment if isinstance(fulfillment, dict) else {}

	oid = cstr(order_id or order.get("id") or fulfillment.get("order_id") or "")
	fid = cstr(fulfillment_id or fulfillment.get("id") or "")

	# Nested order on a rare expanded payload
	if not oid and isinstance(fulfillment.get("order"), dict):
		oid = cstr(fulfillment["order"].get("id") or "")

	if oid and (not order.get("items") or not order.get("fulfillments")):
		fetched = OrderService().get_order(oid, fields=DEFAULT_ORDER_FULFILLMENT_FIELDS)
		if fetched:
			order = fetched
			oid = cstr(order.get("id") or oid)

	if not order and oid:
		order = OrderService().get_order(oid, fields=DEFAULT_ORDER_FULFILLMENT_FIELDS) or {}

	if fid and not fulfillment.get("items"):
		fulfillment = _find_fulfillment_on_order(order, fid) or fulfillment

	if not fid and order.get("fulfillments"):
		# Single-fulfillment convenience: not used by webhooks (they pass ids).
		pass

	if fid and (not fulfillment or fulfillment.get("id") != fid):
		found = _find_fulfillment_on_order(order, fid)
		if found:
			fulfillment = found

	return (order or None), (fulfillment if fulfillment.get("id") else None)


def _find_fulfillment_on_order(order: dict | None, fulfillment_id: str) -> dict | None:
	if not order or not fulfillment_id:
		return None
	for row in order.get("fulfillments") or []:
		if isinstance(row, dict) and cstr(row.get("id")) == cstr(fulfillment_id):
			return row
	return None


def _get_fulfillment_items(dn_items, fulfillment: dict, order: dict, warehouse: str | None) -> list:
	"""Keep SO-mapped DN rows that match this fulfillment; set qty and warehouse.

	Same approach as Shopify ``get_fulfillment_items``: start from mapper output,
	match by ERPNext item code, consume fulfillment lines so partials work.
	"""
	fulfillment_lines = deepcopy(fulfillment.get("items") or [])
	# Pre-resolve fulfillment lines → item_code
	resolved: list[dict] = []
	for fline in fulfillment_lines:
		if not isinstance(fline, dict):
			continue
		item_code = _item_code_for_fulfillment_line(fline, order)
		qty = flt(fline.get("quantity") or 0)
		if not item_code or qty <= 0:
			continue
		resolved.append({"item_code": item_code, "qty": qty, "raw": fline})

	final_items = []

	def take_line(item_code: str):
		for i, line in enumerate(resolved):
			if line["item_code"] == item_code:
				return resolved.pop(i)
		return None

	for dn_item in dn_items:
		match = take_line(dn_item.item_code)
		if not match:
			continue
		update = {"qty": match["qty"]}
		if warehouse:
			update["warehouse"] = warehouse
		final_items.append(dn_item.update(update))

	return final_items


def _item_code_for_fulfillment_line(fline: dict, order: dict) -> str | None:
	"""Map fulfillment item → ERPNext item via order line (variant) or SKU."""
	line_item_id = cstr(fline.get("line_item_id") or "")
	if line_item_id:
		for line in order.get("items") or []:
			if not isinstance(line, dict):
				continue
			if cstr(line.get("id")) == line_item_id:
				code = get_item_code(line)
				if code:
					return code

	sku = cstr(fline.get("sku") or "")
	if sku:
		from medusa_connector.product.item_mapping import get_erpnext_item

		item = get_erpnext_item(sku=sku)
		if item:
			return item.name
		if frappe.db.exists("Item", sku):
			return sku
	return None


def _warehouse_for_fulfillment(fulfillment: dict, settings) -> str | None:
	location_id = cstr(fulfillment.get("location_id") or "")
	if location_id and hasattr(settings, "get_integration_to_erpnext_wh_mapping"):
		wh_map = settings.get_integration_to_erpnext_wh_mapping() or {}
		if location_id in wh_map:
			return wh_map[location_id]
	return settings.get("warehouse")


def _apply_tracking(dn, fulfillment: dict) -> None:
	"""Set transporter / lr_no from fulfillment labels when available."""
	labels = fulfillment.get("labels") or []
	tracking_numbers = []
	for label in labels:
		if not isinstance(label, dict):
			continue
		tn = cstr(label.get("tracking_number") or "")
		if tn:
			tracking_numbers.append(tn)

	if tracking_numbers:
		# ERPNext Delivery Note uses lr_no for lorry receipt / tracking-style refs.
		if hasattr(dn, "lr_no"):
			dn.lr_no = tracking_numbers[0][:140]
		if hasattr(dn, "transporter_name") and len(tracking_numbers) > 1:
			pass
		comment = "Tracking: " + ", ".join(tracking_numbers)
		dn.flags._medusa_tracking_comment = comment


def _update_existing_delivery_note(dn_name: str, order: dict, fulfillment: dict) -> None:
	"""Refresh status / tracking on an already-synced DN (no qty rewrite)."""
	updates = {
		ORDER_STATUS_FIELD: _fulfillment_status_label(order, fulfillment),
	}
	labels = fulfillment.get("labels") or []
	for label in labels:
		if isinstance(label, dict) and label.get("tracking_number"):
			if frappe.get_meta("Delivery Note").has_field("lr_no"):
				updates["lr_no"] = cstr(label.get("tracking_number"))[:140]
			break

	frappe.db.set_value("Delivery Note", dn_name, updates, update_modified=False)


def _fulfillment_status_label(order: dict, fulfillment: dict) -> str:
	parts = []
	if fulfillment.get("canceled_at"):
		parts.append("canceled")
	elif fulfillment.get("delivered_at"):
		parts.append("delivered")
	elif fulfillment.get("shipped_at"):
		parts.append("shipped")
	elif fulfillment.get("packed_at"):
		parts.append("packed")
	else:
		parts.append("fulfilled")
	order_fs = cstr(order.get("fulfillment_status") or "")
	if order_fs:
		parts.append(order_fs)
	return " / ".join(parts)[:140]


def _result(status: str, **kwargs) -> dict[str, Any]:
	out = {"status": status}
	out.update({k: v for k, v in kwargs.items() if v is not None})
	return out
