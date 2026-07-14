# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""App install helpers (custom fields, one-time setup)."""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from medusa_connector.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
)


def _order_id_field(insert_after: str) -> dict:
	return {
		"fieldname": ORDER_ID_FIELD,
		"label": "Medusa Order ID",
		"fieldtype": "Data",
		"insert_after": insert_after,
		"read_only": 1,
		"print_hide": 1,
		"translatable": 0,
		"no_copy": 1,
		"in_standard_filter": 1,
		"search_index": 1,
	}


def _order_number_field(insert_after: str) -> dict:
	return {
		"fieldname": ORDER_NUMBER_FIELD,
		"label": "Medusa Order Number",
		"fieldtype": "Data",
		"insert_after": insert_after,
		"read_only": 1,
		"print_hide": 1,
		"translatable": 0,
		"no_copy": 1,
		"in_list_view": 1,
	}


def _order_status_field(insert_after: str) -> dict:
	return {
		"fieldname": ORDER_STATUS_FIELD,
		"label": "Medusa Order Status",
		"fieldtype": "Data",
		"insert_after": insert_after,
		"read_only": 1,
		"print_hide": 1,
		"translatable": 0,
		"no_copy": 1,
	}


def setup_custom_fields(update: bool = True) -> None:
	"""Ensure identity custom fields for Customer, Address, and sales documents.

	Safe to call repeatedly (``create_custom_fields`` is idempotent with ``update``).
	"""
	custom_fields = {
		"Customer": [
			{
				"fieldname": CUSTOMER_ID_FIELD,
				"label": "Medusa Customer ID",
				"fieldtype": "Data",
				"insert_after": "naming_series",
				"read_only": 1,
				"print_hide": 1,
				"translatable": 0,
				"no_copy": 1,
				"in_standard_filter": 1,
			},
		],
		"Address": [
			{
				"fieldname": ADDRESS_ID_FIELD,
				"label": "Medusa Address ID",
				"fieldtype": "Data",
				"insert_after": "fax",
				"read_only": 1,
				"print_hide": 1,
				"translatable": 0,
				"no_copy": 1,
			},
		],
		"Sales Order": [
			_order_id_field("naming_series"),
			_order_number_field(ORDER_ID_FIELD),
			_order_status_field(ORDER_NUMBER_FIELD),
		],
		"Sales Invoice": [
			_order_id_field("naming_series"),
			_order_number_field(ORDER_ID_FIELD),
			_order_status_field(ORDER_NUMBER_FIELD),
		],
		"Delivery Note": [
			_order_id_field("naming_series"),
			_order_number_field(ORDER_ID_FIELD),
			_order_status_field(ORDER_NUMBER_FIELD),
		],
		"Sales Order Item": [
			{
				"fieldname": ORDER_ITEM_DISCOUNT_FIELD,
				"label": "Medusa Item Discount",
				"fieldtype": "Currency",
				"insert_after": "discount_amount",
				"read_only": 1,
				"print_hide": 1,
				"no_copy": 1,
			},
		],
	}
	create_custom_fields(custom_fields, update=update)


def after_install() -> None:
	setup_custom_fields(update=True)
