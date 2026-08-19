# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for payment, invoice, discount, tax, and shipping mapping."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from medusa_connector.constants import ORDER_ID_FIELD
from medusa_connector.tests.base_test_case import OrderTestCase


class TestPaymentStatusHandling(OrderTestCase):
	def test_unpaid_order_creates_no_invoice(self):
		"""An unpaid order does not create a Sales Invoice."""

		order = self.load_fixture("order_single_item_placed")

		so_name = self.sync(order)

		if so_name:
			self.addCleanup(
				self.cleanup_sales_order,
				so_name,
			)

		self.assertFalse(
			frappe.db.exists(
				"Sales Invoice",
				{ORDER_ID_FIELD: order["id"]},
			)
		)

	def test_paid_order_creates_invoice_and_payment_entry(self):
		"""A captured payment creates an invoice and payment entry."""

		order = self.load_fixture("order_single_item_paid")

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		si_name = frappe.db.get_value(
			"Sales Invoice",
			{
				ORDER_ID_FIELD: order["id"],
				"docstatus": 1,
			},
			"name",
		)

		self.assertIsNotNone(si_name)

		si = frappe.get_doc(
			"Sales Invoice",
			si_name,
		)

		self.assertAlmostEqual(
			flt(si.outstanding_amount),
			0.0,
			places=2,
		)

		pe_name = frappe.db.get_value(
			"Payment Entry",
			{
				ORDER_ID_FIELD: order["id"],
				"docstatus": 1,
			},
			"name",
		)

		self.assertIsNotNone(pe_name)

	def test_partially_paid_order_does_not_yet_invoice(self):
		"""A partially paid order does not create a Sales Invoice."""

		order = self.load_fixture("order_multi_item_paid")

		order["payment_status"] = "partially_captured"
		order["summary"]["paid_total"] = order["total"] / 2

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		self.assertFalse(
			frappe.db.exists(
				"Sales Invoice",
				{ORDER_ID_FIELD: order["id"]},
			)
		)

		self.assertTrue(
			frappe.db.exists(
				"Sales Order",
				{ORDER_ID_FIELD: order["id"]},
			)
		)


class TestDiscountTaxAndShippingMapping(OrderTestCase):
	def test_discount_applied_per_line(self):
		"""Line discounts are reflected in the Sales Order row rate."""

		order = self.load_fixture("order_multi_item_discount_no_sku")

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		so = frappe.get_doc(
			"Sales Order",
			so_name,
		)

		rows_by_item = {row.item_code: row for row in so.items}

		for line in order["items"]:
			item_code = line["variant_sku"] or "CUSTOM-MUG-01"

			row = rows_by_item[item_code]

			expected_gross = flt(line["unit_price"])

			expected_net = max(
				(flt(line["subtotal"]) - flt(line.get("discount_subtotal") or 0)) / line["quantity"],
				0,
			)

			self.assertAlmostEqual(
				flt(row.price_list_rate),
				expected_gross,
				places=2,
			)

			self.assertAlmostEqual(
				flt(row.rate),
				expected_net,
				places=2,
			)

	def test_tax_and_shipping_charges_mapped(self):
		"""Tax and shipping charges are mapped correctly."""

		order = self.load_fixture("order_multi_item_discount_no_sku")

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		so = frappe.get_doc(
			"Sales Order",
			so_name,
		)

		self.assertTrue(so.taxes)

		shipping_method = order["shipping_methods"][0]

		shipping_rows = [
			row
			for row in so.taxes
			if row.account_head == self.shipping_account
			and flt(row.tax_amount) == flt(shipping_method["amount"])
		]

		self.assertEqual(
			len(shipping_rows),
			1,
		)

		tax_rows_total = sum(flt(row.tax_amount) for row in so.taxes)

		expected_total_charges = flt(order["tax_total"]) + flt(shipping_method["amount"])

		self.assertAlmostEqual(
			tax_rows_total,
			expected_total_charges,
			places=2,
		)

		self.assertAlmostEqual(
			flt(so.grand_total),
			flt(order["total"]),
			places=2,
		)

	def test_shipping_without_tax_lines_still_charged(self):
		"""Shipping is charged even without shipping tax lines."""

		order = self.load_fixture("order_multi_item_placed")

		so_name = self.sync(order)

		self.addCleanup(self.cleanup_sales_order, so_name)

		so = frappe.get_doc(
			"Sales Order",
			so_name,
		)

		shipping_amount = order["shipping_methods"][0]["amount"]

		matching = [row for row in so.taxes if flt(row.tax_amount) == flt(shipping_amount)]

		self.assertTrue(matching)

		self.assertAlmostEqual(
			flt(so.grand_total),
			flt(order["total"]),
			places=2,
		)
