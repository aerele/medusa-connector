# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for inbound Medusa → ERPNext product synchronisation."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from medusa_connector.product.webhook import _fetch_map_and_sync
from medusa_connector.tests.utils import TestCase
from medusa_connector.utils.logging import create_medusa_log
from medusa_connector.webhook.dispatch import dispatch_event


class TestProductSync(TestCase):
	"""Inbound sync of Medusa products into ERPNext Items."""

	def _fake_get_product(self, product):
		"""Register the Admin API response for GET /admin/products/{id}."""
		self.fake("GET", f"/admin/products/{product['id']}", json_body={"product": product})

	def test_sync_single_product(self):
		"""A simple Medusa product creates a single ERPNext Item and mapping."""
		product = self.load_fixture("simple_product")
		primary_variant = product["variants"][0]
		self._fake_get_product(product)

		result, mapped = _fetch_map_and_sync(product["id"])

		self.assertEqual(mapped["has_variants"], 0)
		self.assertEqual(result["item_code"], primary_variant["sku"])
		self.assertEqual(result["variant_codes"], [])

		item = frappe.db.get_value(
			"Item",
			result["item_code"],
			["name", "has_variants"],
			as_dict=True,
		)

		self.assertIsNotNone(item)
		self.assertEqual(item.has_variants, 0)

		mapping = frappe.db.get_value(
			"Ecommerce Item",
			{
				"integration": "Medusa Connector",
				"erpnext_item_code": item.name,
			},
			["integration_item_code", "variant_id", "has_variants"],
			as_dict=True,
		)

		self.assertIsNotNone(mapping, "Ecommerce Item mapping should be created")
		self.assertEqual(mapping.integration_item_code, product["id"])
		self.assertEqual(mapping.variant_id, primary_variant["id"])
		self.assertEqual(mapping.has_variants, 0)

	def test_sync_product_with_variants(self):
		"""A variant Medusa product creates an Item Template + Item Variants + mappings."""
		product = self.load_fixture("variant_product")
		self._fake_get_product(product)

		result, mapped = _fetch_map_and_sync(product["id"])

		self.assertEqual(mapped["has_variants"], 1)
		self.assertEqual(len(mapped["variants"]), len(product["variants"]))
		self.assertEqual(len(result["variant_codes"]), len(product["variants"]))

		template = frappe.db.get_value("Item", result["item_code"], ["name", "has_variants"], as_dict=True)

		self.assertIsNotNone(template)
		self.assertEqual(template.name, result["item_code"])
		self.assertEqual(template.has_variants, 1)

		template_mapping = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": "Medusa Connector", "erpnext_item_code": template.name},
			["integration_item_code", "has_variants", "variant_id"],
			as_dict=True,
		)

		self.assertIsNotNone(template_mapping, "Template Ecommerce Item mapping should exist")
		self.assertEqual(template_mapping.integration_item_code, product["id"])
		self.assertEqual(template_mapping.has_variants, 1)
		self.assertFalse(template_mapping.variant_id)

		variant_items = frappe.get_all(
			"Item",
			filters={"name": ["in", result["variant_codes"]]},
			fields=["name", "item_code", "has_variants", "variant_of"],
		)

		self.assertEqual(len(variant_items), len(product["variants"]))

		expected_skus = {variant["sku"] for variant in product["variants"] if variant["sku"]}
		actual_skus = {item.item_code for item in variant_items if item.item_code in expected_skus}
		self.assertEqual(actual_skus, expected_skus)

		for variant_item in variant_items:
			self.assertEqual(variant_item.has_variants, 0)
			self.assertEqual(variant_item.variant_of, template.name)

			variant = next(
				variant
				for variant in product["variants"]
				if variant["sku"] == variant_item.item_code or not variant["sku"]
			)

			mapping = frappe.db.get_value(
				"Ecommerce Item",
				{
					"integration": "Medusa Connector",
					"erpnext_item_code": variant_item.item_code,
				},
				["integration_item_code", "variant_id", "variant_of", "has_variants"],
				as_dict=True,
			)

			self.assertIsNotNone(mapping, f"Ecommerce Item mapping missing for {variant_item.item_code}")
			self.assertEqual(mapping.integration_item_code, product["id"])
			self.assertEqual(mapping.variant_id, variant["id"])
			self.assertEqual(mapping.variant_of, template.name)
			self.assertEqual(mapping.has_variants, 0)

			if variant["sku"]:
				self.assertEqual(variant_item.item_code, variant["sku"])
			else:
				self.assertTrue(variant_item.item_code)
				self.assertNotEqual(variant_item.item_code, variant["id"])

	def test_import_item_from_medusa(self):
		"""Item fields mirror the Medusa product and primary variant."""
		product = self.load_fixture("simple_product")
		primary_variant = product["variants"][0]
		self._fake_get_product(product)

		result, _mapped = _fetch_map_and_sync(product["id"])

		item = frappe.db.get_value(
			"Item",
			result["item_code"],
			[
				"item_code",
				"item_name",
				"description",
				"item_group",
				"weight_per_unit",
				"standard_rate",
			],
			as_dict=True,
		)

		self.assertIsNotNone(item)
		self.assertEqual(item.item_name, product["title"])
		self.assertIn(product["subtitle"], item.description)
		self.assertIn(product["description"], item.description)
		self.assertEqual(item.item_code, primary_variant["sku"])
		self.assertEqual(item.item_group, product["categories"][0]["name"])
		self.assertEqual(flt(item.weight_per_unit), flt(primary_variant["weight"]))

		usd_price = next(
			price["amount"] for price in primary_variant["prices"] if price["currency_code"] == "usd"
		)
		self.assertEqual(flt(item.standard_rate), flt(usd_price))

	def test_sync_missing_product(self):
		"""A missing Medusa product fails the sync and creates no mapping."""
		missing_product_id = "prod_DOES_NOT_EXIST"
		event_name = "product.updated"
		event_id = f"{event_name}:{missing_product_id}"

		self.fake(
			"GET",
			f"/admin/products/{missing_product_id}",
			status=404,
			json_body={
				"type": "not_found",
				"message": f"Product with id: {missing_product_id} was not found",
			},
		)

		log = create_medusa_log(
			status="Queued",
			method="medusa_connector.webhook.dispatch.dispatch_event",
			message=f"webhook:{event_id}",
			request_data={
				"event_id": event_id,
				"event_name": event_name,
				"payload": {"id": missing_product_id},
			},
			make_new=True,
		)

		with self.assertRaises(Exception):
			dispatch_event(log_name=log.name)

		log_status = frappe.db.get_value(
			"Ecommerce Integration Log",
			log.name,
			"status",
		)
		self.assertEqual(log_status, "Error")

		mapping = frappe.db.exists(
			"Ecommerce Item",
			{
				"integration": "Medusa Connector",
				"integration_item_code": missing_product_id,
			},
		)
		self.assertIsNone(
			mapping,
			"No Ecommerce Item mapping should be created for a missing product",
		)

	def test_variant_id_mapping(self):
		"""Each synced ERPNext variant is mapped to its correct Medusa variant_id."""
		product = self.load_fixture("variant_product")
		self._fake_get_product(product)

		result, _mapped = _fetch_map_and_sync(product["id"])

		self.assertEqual(len(result["variant_codes"]), len(product["variants"]))

		items = frappe.get_all(
			"Item",
			filters={"name": ["in", result["variant_codes"]]},
			fields=["name", "item_code", "has_variants", "variant_of"],
		)

		mappings = frappe.get_all(
			"Ecommerce Item",
			filters={
				"integration": "Medusa Connector",
				"erpnext_item_code": ["in", result["variant_codes"]],
			},
			fields=["erpnext_item_code", "integration_item_code", "variant_id", "variant_of", "has_variants"],
		)

		items_by_code = {item.item_code: item for item in items}
		mappings_by_item = {mapping.erpnext_item_code: mapping for mapping in mappings}

		expected_variant_ids = {
			variant["sku"]: variant["id"] for variant in product["variants"] if variant.get("sku")
		}

		no_sku_variant = next(variant for variant in product["variants"] if not variant.get("sku"))

		for variant_code in result["variant_codes"]:
			item = items_by_code.get(variant_code)
			self.assertIsNotNone(item, f"ERPNext Item not found for {variant_code}")

			self.assertEqual(item.name, variant_code)
			self.assertEqual(item.item_code, variant_code)
			self.assertEqual(item.has_variants, 0)
			self.assertEqual(item.variant_of, result["item_code"])

			mapping = mappings_by_item.get(item.item_code)
			self.assertIsNotNone(mapping, f"Ecommerce Item mapping missing for {item.item_code}")

			self.assertEqual(mapping.integration_item_code, product["id"])
			self.assertEqual(mapping.variant_of, result["item_code"])
			self.assertEqual(mapping.has_variants, 0)

			if item.item_code in expected_variant_ids:
				self.assertEqual(
					mapping.variant_id,
					expected_variant_ids[item.item_code],
					f"Mismatched Medusa variant_id for SKU {item.item_code}",
				)
			else:
				self.assertEqual(
					mapping.variant_id,
					no_sku_variant["id"],
					"Variant without SKU should be mapped using its Medusa variant_id",
				)
