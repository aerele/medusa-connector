# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""App install helpers for custom fields and one-time setup."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from medusa_connector.constants import (
	ADDRESS_ID_FIELD,
	CLAIM_ID_FIELD,
	CUSTOMER_ID_FIELD,
	EXCHANGE_ID_FIELD,
	FULFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	REFUND_ID_FIELD,
	RETURN_ID_FIELD,
)

_ID_FIELD_DEFS: dict[str, dict] = {
	CUSTOMER_ID_FIELD: {"label": "Medusa Customer ID", "searchable": True},
	ADDRESS_ID_FIELD: {"label": "Medusa Address ID"},
	ORDER_ID_FIELD: {"label": "Medusa Order ID", "searchable": True},
	ORDER_NUMBER_FIELD: {"label": "Medusa Order Number", "extra": {"in_list_view": 1}},
	ORDER_STATUS_FIELD: {"label": "Medusa Order Status"},
	REFUND_ID_FIELD: {"label": "Medusa Refund ID", "searchable": True},
	RETURN_ID_FIELD: {"label": "Medusa Return ID", "searchable": True},
	CLAIM_ID_FIELD: {"label": "Medusa Claim ID", "searchable": True},
	EXCHANGE_ID_FIELD: {"label": "Medusa Exchange ID", "searchable": True},
	FULFILLMENT_ID_FIELD: {"label": "Medusa Fulfillment ID", "searchable": True},
}


def _medusa_id_field(fieldname: str, label: str, insert_after: str, *, searchable: bool = False) -> dict:
	"""Build a read-only Medusa ID custom field."""
	field = {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": "Data",
		"insert_after": insert_after,
		"read_only": 1,
		"print_hide": 1,
		"translatable": 0,
		"no_copy": 1,
	}

	if searchable:
		field.update(
			{
				"in_standard_filter": 1,
				"search_index": 1,
			}
		)

	return field


def _registered_id_field(fieldname: str, insert_after: str) -> dict:
	"""Build a Medusa ID field looked up from the shared registry."""
	spec = _ID_FIELD_DEFS[fieldname]
	field = _medusa_id_field(
		fieldname,
		spec["label"],
		insert_after,
		searchable=spec.get("searchable", False),
	)
	field.update(spec.get("extra", {}))
	return field


def _chained_id_fields(fieldnames: list[str], start_after: str) -> list[dict]:
	"""Build a sequence of registered Medusa ID fields, each inserted after the previous one.

	`start_after` is the anchor for the first field; each subsequent field is
	inserted after the previous field in the list. This avoids having to
	manually rewire insert_after references whenever the sequence changes.
	"""
	fields = []
	insert_after = start_after
	for fieldname in fieldnames:
		fields.append(_registered_id_field(fieldname, insert_after))
		insert_after = fieldname
	return fields


def _item_dimension_field(fieldname: str, label: str, insert_after: str) -> dict:
	return {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": "Float",
		"insert_after": insert_after,
	}


def _item_dimension_fields() -> list[dict]:
	"""Build the chained length/width/height custom fields for Item."""
	dimensions = [
		("medusa_custom_length", "Length (Medusa)"),
		("medusa_custom_width", "Width (Medusa)"),
		("medusa_custom_height", "Height (Medusa)"),
	]
	fields = []
	insert_after = "weight_uom"
	for fieldname, label in dimensions:
		fields.append(_item_dimension_field(fieldname, label, insert_after))
		insert_after = fieldname
	return fields


def _order_item_discount_field() -> dict:
	return {
		"fieldname": ORDER_ITEM_DISCOUNT_FIELD,
		"label": "Medusa Item Discount",
		"fieldtype": "Currency",
		"insert_after": "discount_amount",
		"read_only": 1,
		"print_hide": 1,
		"no_copy": 1,
	}


def setup_custom_fields(update: bool = True) -> None:
	"""Ensure all Medusa Connector custom fields exist."""
	custom_fields = {
		"Customer": [_registered_id_field(CUSTOMER_ID_FIELD, "naming_series")],
		"Address": [_registered_id_field(ADDRESS_ID_FIELD, "fax")],
		"Item": _item_dimension_fields(),
		"Sales Order": _chained_id_fields(
			[ORDER_ID_FIELD, ORDER_NUMBER_FIELD, ORDER_STATUS_FIELD],
			"naming_series",
		),
		"Sales Invoice": _chained_id_fields(
			[
				ORDER_ID_FIELD,
				ORDER_NUMBER_FIELD,
				ORDER_STATUS_FIELD,
				REFUND_ID_FIELD,
				CLAIM_ID_FIELD,
			],
			"naming_series",
		),
		"Delivery Note": _chained_id_fields(
			[
				ORDER_ID_FIELD,
				ORDER_NUMBER_FIELD,
				ORDER_STATUS_FIELD,
				FULFILLMENT_ID_FIELD,
				RETURN_ID_FIELD,
				CLAIM_ID_FIELD,
				EXCHANGE_ID_FIELD,
			],
			"naming_series",
		),
		"Sales Order Item": [_order_item_discount_field()],
		"Payment Entry": _chained_id_fields(
			[ORDER_ID_FIELD, REFUND_ID_FIELD, CLAIM_ID_FIELD],
			"reference_no",
		),
	}

	create_custom_fields(custom_fields, update=update)


def after_install() -> None:
	"""Create Medusa Connector custom fields after app installation."""
	try:
		setup_custom_fields(update=True)
	except Exception:
		frappe.log_error(
			title="Medusa Connector after_install failed",
			message=frappe.get_traceback(with_context=True),
		)
		raise
