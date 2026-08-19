# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for customer and address mapping."""

from __future__ import annotations

import frappe

from medusa_connector.tests.base_test_case import OrderTestCase


class TestCustomerAndAddressMapping(OrderTestCase):
	def test_customer_created_and_linked(self):
		"""A Medusa customer is created and linked to the Sales Order."""

		order = self.load_fixture("order_single_item_placed")

		so = frappe.get_doc(
			"Sales Order",
			self.sync(order),
		)

		self.assertTrue(
			frappe.db.exists(
				"Customer",
				so.customer,
			)
		)

		customer = order["customer"]

		customer_name = customer["first_name"]

		if customer.get("last_name"):
			customer_name += f" {customer['last_name']}"

		self.assertEqual(
			frappe.db.get_value(
				"Customer",
				so.customer,
				"customer_name",
			),
			customer_name,
		)

	def test_billing_and_shipping_address_mapped(self):
		"""Billing and shipping addresses are mapped to the Sales Order."""
		order = self.load_fixture("order_single_item_placed")

		so = frappe.get_doc("Sales Order", self.sync(order))

		billing = frappe.get_doc("Address", so.customer_address)
		shipping = frappe.get_doc("Address", so.shipping_address_name)

		expected_billing = order["billing_address"]
		expected_shipping = order["shipping_address"]

		self.assertEqual(billing.address_line1, expected_billing["address_1"])
		self.assertEqual(billing.address_line2, expected_billing["address_2"])
		self.assertEqual(billing.city, expected_billing["city"])
		self.assertEqual(billing.state, expected_billing["province"])
		self.assertEqual(billing.address_type, "Billing")

		self.assertEqual(shipping.address_line1, expected_shipping["address_1"])
		self.assertEqual(shipping.address_line2, expected_shipping["address_2"])
		self.assertEqual(shipping.city, expected_shipping["city"])
		self.assertEqual(shipping.state, expected_shipping["province"])
		self.assertEqual(shipping.address_type, "Shipping")
