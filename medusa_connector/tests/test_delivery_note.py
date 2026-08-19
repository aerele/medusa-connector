# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for order cancellation, retry, and complete lifecycle."""

from __future__ import annotations

import frappe

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	ORDER_STATUS_FIELD,
)
from medusa_connector.order._shared import status_label
from medusa_connector.tests.base_test_case import OrderTestCase


class TestOrderCancellation(OrderTestCase):
	def test_cancel_reverses_payment_and_cancels_sales_order(self):
		"""A paid order is cancelled with its payment and invoice reversed."""

		placed = self.load_fixture("order_single_item_full_cycle_placed.json")

		product_id = placed["items"][0]["product_id"]

		product = self.load_fixture(product_id)

		self.fake(
			"GET",
			f"/admin/products/{product_id}",
			json_body={
				"product": product,
			},
		)

		so_name = self.sync(placed)

		paid = self.load_fixture("order_single_item_full_cycle_paid")

		self.sync(paid)

		self.addCleanup(self.cleanup_sales_order, so_name)

		si_name = frappe.db.get_value(
			"Sales Invoice",
			{
				ORDER_ID_FIELD: paid["id"],
				"docstatus": 1,
			},
			"name",
		)

		pe_name = frappe.db.get_value(
			"Payment Entry",
			{
				ORDER_ID_FIELD: paid["id"],
				"docstatus": 1,
			},
			"name",
		)

		self.assertIsNotNone(si_name)
		self.assertIsNotNone(pe_name)

		cancelled = self.load_fixture("order_single_item_full_cycle_canceled")

		payment_id = cancelled["payment_collections"][0]["payments"][0]["id"]

		self.fake(
			"GET",
			f"/admin/payments/{payment_id}",
			json_body={
				"payment": {
					"id": payment_id,
					"order_id": cancelled["id"],
					"refunds": [],
				}
			},
		)

		so_name = self.cancel(cancelled)

		self.assertIsNotNone(so_name)

		self.assertEqual(
			frappe.db.get_value(
				"Sales Order",
				so_name,
				"docstatus",
			),
			2,
		)

		self.assertEqual(
			frappe.db.get_value(
				"Sales Invoice",
				si_name,
				"docstatus",
			),
			2,
		)

		self.assertEqual(
			frappe.db.get_value(
				"Payment Entry",
				pe_name,
				"docstatus",
			),
			2,
		)

		self.assertEqual(
			frappe.db.get_value(
				"Sales Order",
				so_name,
				ORDER_STATUS_FIELD,
			),
			status_label(cancelled),
		)


class TestAPIFailureAndRetryHandling(OrderTestCase):
	def test_sync_recovers_on_retry_after_transient_product_api_failure(self):
		"""A transient product API failure can be recovered by retrying."""

		order = self.load_fixture("order_retry_after_transient_product_fetch_error")

		line = order["items"][0]
		product_id = line["product_id"]

		self.fake(
			"GET",
			f"/admin/products/{product_id}",
			status=500,
			json_body={"message": "internal error"},
		)

		first_attempt = self.sync(order)

		self.assertIsNone(first_attempt)

		self.assertFalse(
			frappe.db.exists(
				"Sales Order",
				{ORDER_ID_FIELD: order["id"]},
			)
		)

		product_response = self.load_fixture(product_id)

		self.fake(
			"GET",
			f"/admin/products/{product_id}",
			json_body={
				"product": product_response,
			},
		)

		second_attempt = self.sync(order)
		if second_attempt:
			self.addCleanup(self.cleanup_sales_order, second_attempt)

		self.assertIsNotNone(second_attempt)
		self.assertEqual(
			frappe.db.count(
				"Sales Order",
				{ORDER_ID_FIELD: order["id"]},
			),
			1,
		)


class TestCompleteOrderLifecycleFlow(OrderTestCase):
	def test_placed_paid_fulfilled_delivered(self):
		"""One order progresses through the complete lifecycle."""

		placed = self.load_fixture("order_multi_item_placed")

		so_name = self.sync(placed)

		self.addCleanup(self.cleanup_sales_order, so_name)

		self.assertIsNotNone(so_name)

		paid = self.load_fixture("order_multi_item_paid")

		self.sync(paid)

		si_name = frappe.db.get_value(
			"Sales Invoice",
			{
				ORDER_ID_FIELD: paid["id"],
				"docstatus": 1,
			},
			"name",
		)

		self.assertIsNotNone(si_name)

		fulfilled = self.load_fixture("order_multi_item_fulfilled")

		self.sync(fulfilled)

		dn_name = frappe.db.get_value(
			"Delivery Note",
			{
				ORDER_ID_FIELD: fulfilled["id"],
				"docstatus": 1,
			},
			"name",
		)

		self.assertIsNotNone(dn_name)

		delivered = self.load_fixture("order_multi_item_delivered")

		self.sync(delivered)

		self.assertEqual(
			frappe.db.count(
				"Sales Order",
				{ORDER_ID_FIELD: placed["id"]},
			),
			1,
		)

		self.assertEqual(
			frappe.db.get_value(
				"Sales Order",
				{ORDER_ID_FIELD: placed["id"]},
				"name",
			),
			so_name,
		)

		self.assertEqual(
			frappe.db.get_value(
				"Sales Order",
				so_name,
				ORDER_STATUS_FIELD,
			),
			status_label(delivered),
		)
