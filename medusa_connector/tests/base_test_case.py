# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Base test case for Medusa order synchronization tests."""

from __future__ import annotations

import frappe

from medusa_connector.constants import ORDER_ID_FIELD, SETTING_DOCTYPE
from medusa_connector.order.sync import OrderSync
from medusa_connector.tests.utils import TestCase


class OrderTestCase(TestCase):
	"""Shared ERPNext master data and Medusa Settings setup for order tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		cls.fixture_items = [
			{
				"item_code": "TEST_ORDER-123",
				"item_name": "test order item 1",
				"product_id": "prod_01KZ37YA6B4XGZ7G7MV3AP382D",
				"variant_id": "variant_01KZ37YA8YDS9MDCNEQEY7KZRT",
				"hsn_code": "999800",
			},
			{
				"item_code": "HEL-BLACk-01",
				"item_name": "Helmat",
				"product_id": "prod_01KZ3GPJ5PWY1E5549RFVFGA5N",
				"variant_id": "variant_01KZ3GPJ8DQJWZFTCJVGSECM61",
				"hsn_code": "999800",
			},
			{
				"item_code": "HEL-WHITE-01",
				"item_name": "Helmat",
				"product_id": "prod_01KZ3GPJ5PWY1E5549RFVFGA5N",
				"variant_id": "variant_01KZ3GPJ8DZ8H7H1PBP37196XD",
				"hsn_code": "999800",
			},
			{
				"item_code": "CUSTOM-MUG-01",
				"item_name": "Custom Engraved Mug",
				"product_id": "prod_01KZ3HQK3JZKF0MYX5AS8BCFQD",
				"variant_id": "variant_01KZ3HQK5TB6Y9TBST6QYGJKMH",
				"hsn_code": "999800",
			},
		]

		cls.fixture_item_codes = [item["item_code"] for item in cls.fixture_items]

		cls._discover_or_create_masters()
		cls._configure_order_settings()
		cls._create_item_mappings()

	@classmethod
	def tearDownClass(cls):
		"""Clean up shared test fixtures created in setUpClass."""

		for item_code in cls.fixture_item_codes:
			mapping = frappe.db.get_value(
				"Ecommerce Item",
				{
					"integration": "Medusa Connector",
					"erpnext_item_code": item_code,
				},
				"name",
			)

			if mapping:
				frappe.delete_doc("Ecommerce Item", mapping, force=True)

			if frappe.db.exists("Item", item_code):
				frappe.delete_doc("Item", item_code, force=True)

		super().tearDownClass()

	# ERPNext master data
	@classmethod
	def _discover_or_create_masters(cls):
		"""Reuse common ERPNext masters and create only missing test data."""

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)

		if not cls.company:
			raise AssertionError("No Company found. before_tests() should create the test Company.")

		cls.warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"company": cls.company,
				"is_group": 0,
			},
			"name",
		)

		if not cls.warehouse:
			cls.warehouse = cls._create_warehouse(cls.company)

		cls.cost_center = frappe.db.get_value(
			"Cost Center",
			{
				"company": cls.company,
				"is_group": 0,
			},
			"name",
		)

		if not cls.cost_center:
			cls.cost_center = cls._create_cost_center(cls.company)

		cls.tax_account = frappe.db.get_value(
			"Account",
			{
				"company": cls.company,
				"account_name": "Output Tax GST",
				"is_group": 0,
			},
			"name",
		)

		if not cls.tax_account:
			raise AssertionError("Output Tax GST account was not found. before_tests() should create it.")

		cls.shipping_account = cls._find_account(
			cls.company,
			"Medusa Test Shipping Income",
		)

		if not cls.shipping_account:
			cls.shipping_account = cls._create_account(
				cls.company,
				"Medusa Test Shipping Income",
				"Income",
			)

		cls.cash_account = frappe.db.get_value(
			"Account",
			{
				"company": cls.company,
				"account_type": "Cash",
				"is_group": 0,
			},
			"name",
		)

		if not cls.cash_account:
			cls.cash_account = frappe.db.get_value(
				"Account",
				{
					"company": cls.company,
					"account_type": "Bank",
					"is_group": 0,
				},
				"name",
			)

		if not cls.cash_account:
			cls.cash_account = cls._find_account(
				cls.company,
				"Medusa Test Cash",
			)

		if not cls.cash_account:
			cls.cash_account = cls._create_account(
				cls.company,
				"Medusa Test Cash",
				"Asset",
				"Cash",
			)

	@classmethod
	def _create_warehouse(cls, company):
		"""Create a warehouse only when no usable warehouse exists."""

		warehouse_name = "Medusa Test Store"

		existing = frappe.db.get_value(
			"Warehouse",
			{
				"warehouse_name": warehouse_name,
				"company": company,
			},
			"name",
		)

		if existing:
			return existing

		doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": warehouse_name,
				"company": company,
			}
		)

		doc.insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def _create_cost_center(cls, company):
		"""Create a cost center only when no usable cost center exists."""

		cost_center_name = "Medusa Test CC"

		existing = frappe.db.get_value(
			"Cost Center",
			{
				"cost_center_name": cost_center_name,
				"company": company,
			},
			"name",
		)

		if existing:
			return existing

		doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": cost_center_name,
				"company": company,
			}
		)

		doc.insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def _find_account(cls, company, account_name):
		"""Find an existing account by exact name and company."""

		return frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_name": account_name,
				"is_group": 0,
			},
			"name",
		)

	@classmethod
	def _create_account(
		cls,
		company,
		account_name,
		root_type,
		account_type=None,
	):
		"""Create a test account if it does not already exist."""

		existing = cls._find_account(company, account_name)

		if existing:
			return existing

		parent = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"root_type": root_type,
				"is_group": 1,
			},
			"name",
		)

		if not parent:
			raise AssertionError(f"No {root_type} group account found for company {company}.")

		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"company": company,
				"root_type": root_type,
				"parent_account": parent,
				"account_type": account_type,
				"is_group": 0,
			}
		)

		doc.insert(ignore_permissions=True)
		return doc.name

	# Medusa Settings
	@classmethod
	def _configure_order_settings(cls):
		"""Configure Medusa Settings using shared test masters."""

		settings = frappe.get_doc(SETTING_DOCTYPE)

		settings.company = cls.company
		settings.warehouse = cls.warehouse
		settings.cost_center = cls.cost_center
		settings.default_sales_tax_account = cls.tax_account
		settings.default_shipping_charges_account = cls.shipping_account
		settings.cash_bank_account = cls.cash_account

		settings.sync_sales_invoice = 1
		settings.sync_delivery_note = 1
		settings.customer_group = "Individual"
		settings.default_customer = None
		settings.add_shipping_as_item = 0
		settings.consolidate_taxes = 0

		settings.flags.ignore_validate = True
		settings.flags.ignore_mandatory = True
		settings.save()

		cls.settings = settings

	@classmethod
	def get_settings(cls):
		return frappe.get_doc(SETTING_DOCTYPE)

	# Item mappings
	@classmethod
	def _create_item_mappings(cls):
		"""Create ERPNext Items and Medusa Ecommerce Item mappings."""

		for item in cls.fixture_items:
			cls._map_item(**item)

	@classmethod
	def _map_item(
		cls,
		item_code,
		item_name,
		product_id,
		variant_id,
		hsn_code=None,
	):
		"""Create an ERPNext Item and Ecommerce Item mapping if missing."""

		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_name,
					"item_group": "All Item Groups",
					"is_stock_item": 0,
					"stock_uom": "Nos",
				}
			)

			if hsn_code and frappe.db.has_column("Item", "gst_hsn_code"):
				item.gst_hsn_code = hsn_code

			item.flags.ignore_mandatory = True
			item.insert(ignore_permissions=True)

		elif hsn_code and frappe.db.has_column("Item", "gst_hsn_code"):
			if not frappe.db.get_value(
				"Item",
				item_code,
				"gst_hsn_code",
			):
				frappe.db.set_value(
					"Item",
					item_code,
					"gst_hsn_code",
					hsn_code,
				)

		if not frappe.db.exists(
			"Ecommerce Item",
			{
				"integration": "Medusa Connector",
				"erpnext_item_code": item_code,
				"variant_id": variant_id,
			},
		):
			mapping = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": "Medusa Connector",
					"erpnext_item_code": item_code,
					"integration_item_code": product_id,
					"variant_id": variant_id,
					"has_variants": 0,
					"item_synced_on": frappe.utils.now(),
				}
			)

			mapping.flags.ignore_mandatory = True
			mapping.insert(ignore_permissions=True)

	# Sync helpers
	def sync(self, order):
		"""Synchronize one Medusa order payload."""

		return OrderSync(self.get_settings()).sync(order)

	def cancel(self, order):
		"""Cancel one Medusa order payload."""

		return OrderSync(self.get_settings()).cancel(order)

	def cleanup_sales_order(self, sales_order):
		if not frappe.db.exists("Sales Order", sales_order):
			return

		def cancel_and_delete(doctype, name):
			if not name:
				return

			doc = frappe.get_doc(doctype, name)

			if doc.docstatus == 1:
				doc.cancel()

			frappe.delete_doc(doctype, name, force=True)

		invoices = frappe.get_all(
			"Sales Invoice Item",
			filters={"sales_order": sales_order},
			pluck="parent",
			distinct=True,
		)

		for invoice in invoices:
			for pe in frappe.get_all(
				"Payment Entry Reference",
				filters={
					"reference_doctype": "Sales Invoice",
					"reference_name": invoice,
				},
				pluck="parent",
			):
				cancel_and_delete("Payment Entry", pe)

			cancel_and_delete("Sales Invoice", invoice)

		for dn in frappe.get_all(
			"Delivery Note Item",
			filters={"against_sales_order": sales_order},
			pluck="parent",
			distinct=True,
		):
			cancel_and_delete("Delivery Note", dn)

		cancel_and_delete("Sales Order", sales_order)
