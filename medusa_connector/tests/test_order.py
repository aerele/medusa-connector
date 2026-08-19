# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Permanent tests for core Medusa -> ERPNext order synchronization."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from medusa_connector.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
)
from medusa_connector.order._shared import status_label
from medusa_connector.order.sales_order_sync import SalesOrderSync
from medusa_connector.tests.base_test_case import OrderTestCase


class TestSingleItemOrderSync(OrderTestCase):
	def test_single_item_order_creation_mapping_and_status_sync(self):
		"""A single-item Medusa order is created correctly and updates its status on resync."""

		order = self.load_fixture("order_single_item_placed")

		product = self.load_fixture("product_single_item_order")

		self.fake(
			"GET",
			f"/admin/products/{order['items'][0]['product_id']}",
			json_body={"product": product},
		)

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		self.assertIsNotNone(so_name)

		so = frappe.get_doc("Sales Order", so_name)

		# Sales Order creation
		self.assertEqual(so.docstatus, 1)
		self.assertTrue(so.customer)
		self.assertEqual(
			so.get(ORDER_ID_FIELD),
			order["id"],
		)
		self.assertEqual(
			so.get(ORDER_NUMBER_FIELD),
			str(order["display_id"]),
		)
		self.assertEqual(
			so.get(ORDER_STATUS_FIELD),
			status_label(order),
		)

		# Line item mapping
		self.assertEqual(len(so.items), 1)

		row = so.items[0]
		line = order["items"][0]

		self.assertEqual(
			row.item_code,
			line["variant_sku"],
		)
		self.assertEqual(
			row.qty,
			line["quantity"],
		)
		self.assertEqual(
			flt(row.rate),
			flt(line["unit_price"]),
		)
		self.assertEqual(
			flt(row.amount),
			flt(line["unit_price"]) * line["quantity"],
		)

		# Order total
		self.assertAlmostEqual(
			flt(so.grand_total),
			flt(order["total"]),
			places=2,
		)

		# Status sync
		updated_order = self.load_fixture("order_single_item_paid")

		self.assertEqual(
			order["id"],
			updated_order["id"],
			msg=(
				"Fixtures must represent the same Medusa order. "
				f"Expected id '{order['id']}', but got '{updated_order['id']}'."
			),
		)

		self.sync(updated_order)
		so.reload()

		self.assertEqual(
			so.get(ORDER_STATUS_FIELD),
			status_label(updated_order),
		)


class TestMultiItemOrderSync(OrderTestCase):
	def test_multi_item_order_sync_and_duplicate_prevention(self):
		"""A multi-item Medusa order maps correctly and does not create duplicates on resync."""

		order = self.load_fixture("order_multi_item_placed")

		so_name = self.sync(order)

		self.addCleanup(
			self.cleanup_sales_order,
			so_name,
		)

		so = frappe.get_doc(
			"Sales Order",
			so_name,
		)

		# Order identifiers
		self.assertEqual(
			so.get(ORDER_ID_FIELD),
			order["id"],
		)
		self.assertEqual(
			so.get(ORDER_NUMBER_FIELD),
			str(order["display_id"]),
		)

		fetched = SalesOrderSync.get_sales_order_doc(order["id"])

		self.assertIsNotNone(fetched)
		self.assertEqual(
			fetched.name,
			so_name,
		)

		# Item mapping
		self.assertEqual(
			len(so.items),
			len(order["items"]),
		)

		qty_by_item = {row.item_code: row.qty for row in so.items}

		for line in order["items"]:
			self.assertIn(
				line["variant_sku"],
				qty_by_item,
			)
			self.assertEqual(
				qty_by_item[line["variant_sku"]],
				line["quantity"],
			)

		# Order total
		self.assertAlmostEqual(
			flt(so.grand_total),
			flt(order["total"]),
			places=2,
		)

		# Duplicate prevention
		second = self.sync(order)

		self.assertEqual(
			so_name,
			second,
		)

		self.assertEqual(
			frappe.db.count(
				"Sales Order",
				{ORDER_ID_FIELD: order["id"]},
			),
			1,
		)


class TestMissingProductOrVariantHandling(OrderTestCase):
	def test_missing_product_fails_gracefully(self):
		"""A missing Medusa product does not create a Sales Order."""

		order = self.load_fixture("order_with_nonexistent_product")

		product_id = order["items"][0]["product_id"]

		self.fake(
			"GET",
			f"/admin/products/{product_id}",
			status=404,
			json_body={
				"message": "not found",
			},
		)

		result = self.sync(order)

		self.assertIsNone(result)

		self.assertFalse(
			frappe.db.exists(
				"Sales Order",
				{ORDER_ID_FIELD: order["id"]},
			)
		)


class TestMissingSKUHandling(OrderTestCase):
	def test_item_without_sku_resolves_via_variant_id(self):
		"""A missing SKU is resolved using the Medusa variant mapping."""

		order = self.load_fixture("order_multi_item_discount_no_sku")

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		so = frappe.get_doc(
			"Sales Order",
			so_name,
		)

		no_sku_line = next(line for line in order["items"] if line["variant_sku"] is None)

		matching_rows = [row for row in so.items if row.item_code == "CUSTOM-MUG-01"]
		# 1. Sales Order created
		self.assertTrue(so.name)

		# 2. Same number of items
		self.assertEqual(len(so.items), len(order["items"]))

		# 3. Missing SKU item resolved correctly
		self.assertEqual(len(matching_rows), 1)
		self.assertEqual(matching_rows[0].item_code, "CUSTOM-MUG-01")

		# 4. Quantity preserved
		self.assertEqual(
			matching_rows[0].qty,
			no_sku_line["quantity"],
		)

		# 5. Price preserved
		self.assertEqual(
			flt(matching_rows[0].rate),
			flt(no_sku_line["unit_price"]),
		)

		# 6. Overall order total matches
		self.assertAlmostEqual(
			flt(so.grand_total),
			flt(order["total"]),
			places=2,
		)
