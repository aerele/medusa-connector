# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Test utilities for Medusa connector tests."""

from __future__ import annotations

import copy
import json
import os
from typing import ClassVar

import frappe
import responses
from frappe.tests import IntegrationTestCase

from medusa_connector.constants import MODULE_NAME, SETTING_DOCTYPE
from medusa_connector.medusa.client import MedusaClient


class TestCase(IntegrationTestCase):
	"""Base test case class for Medusa connector tests.

	Follows the same pattern as shopify and unicommerce integrations.
	"""

	config: ClassVar = {
		"enabled": 1,
		"medusa_base_url": "https://medusa.test.com",
		"api_key": "test_api_key",
		"upload_erpnext_items": 1,
		"update_erpnext_stock_levels_to_medusa": 1,
		"sync_new_item_as_published": 0,
		"upload_variants_as_items": 1,
		"default_currency": "usd",
		"default_sales_channel_id": "sc_01TEST",
	}

	@classmethod
	def setUpClass(cls):
		# Call parent first to auto-generate standard test records
		super().setUpClass()

		# Configure Medusa settings
		settings = frappe.get_doc(SETTING_DOCTYPE)

		# Remember existing config
		cls.old_config = copy.deepcopy(cls.config)
		for key in cls.old_config:
			cls.old_config[key] = getattr(settings, key, None)

		# Apply test config
		for key, value in cls.config.items():
			setattr(settings, key, value)

		settings.flags.ignore_validate = True  # prevent hitting actual API
		settings.flags.ignore_mandatory = True
		settings.save()

		# Clear default warehouse to avoid company mismatch in test items
		frappe.db.set_default("default_warehouse", "")

	@classmethod
	def tearDownClass(cls):
		# Restore original config
		settings = frappe.get_doc(SETTING_DOCTYPE)
		for key, value in cls.old_config.items():
			if value is not None:
				setattr(settings, key, value)

		settings.flags.ignore_validate = True
		settings.flags.ignore_mandatory = True
		settings.save()

	def setUp(self):
		# HTTP-level fake for the Medusa Admin API, mirroring the pattern used
		# by the Shopify integration's ``fake()`` helper (there built on
		# pyactiveresource's ``http_fake`` shim). Medusa's client is plain
		# ``requests``, so ``responses`` is the equivalent transport-level
		# mock: real ``MedusaClient``/``ProductService`` code runs unchanged,
		# only the outbound HTTP call is intercepted.
		self.responses = responses.RequestsMock(assert_all_requests_are_fired=False)
		self.responses.start()

		self.addCleanup(self.responses.stop)
		self.addCleanup(self.responses.reset)

	def load_fixture(self, name):
		"""Load JSON fixture from fixtures directory."""
		fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", f"{name}.json")
		with open(fixture_path) as f:
			return json.load(f)

	def fake(self, method, path, *, fixture=None, json_body=None, status=200, base_url=None):
		"""Register one fake Medusa Admin API HTTP response.

		Args:
		    method: HTTP method, e.g. "GET", "POST", "DELETE".
		    path: REST path relative to the Medusa base URL,
		        e.g. "/admin/products/prod_123". Query strings are ignored
		        when matching, so callers don't need to replicate params
		        like ``fields=...``.
		    fixture: Optional fixture name (as passed to ``load_fixture``)
		        used as the JSON response body.
		    json_body: Optional explicit JSON-serialisable response body.
		        Takes precedence over ``fixture`` when both are given.
		    status: HTTP status code to respond with (default 200).
		    base_url: Override the Medusa base URL (defaults to the
		        configured ``medusa_base_url`` test setting).

		Returns the registered ``responses`` entry.
		"""
		body = json_body if json_body is not None else (self.load_fixture(fixture) if fixture else None)

		base_url = base_url or self.config["medusa_base_url"]
		url = f"{base_url}/{path.lstrip('/')}"

		return self.responses.add(
			getattr(responses, method.upper()),
			url,
			status=status,
			json=body,
		)

	def create_test_item(self, item_code=None, **kwargs):
		"""Create a test Item document with common defaults.

		Args:
		    item_code: Optional item code, will generate if not provided
		    **kwargs: Additional fields to set on the item

		Returns:
		    The created Item document
		"""
		if not item_code:
			item_code = f"TEST-{frappe.generate_hash(length=8)}"

		# Default item values
		item_data = {
			"doctype": "Item",
			"item_code": item_code,
			"item_name": f"Test Item {item_code}",
			"description": f"Test description for {item_code}",
			"item_group": "Products",
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"standard_rate": 10.0,
		}

		# Merge with provided kwargs
		item_data.update(kwargs)

		# Handle item_defaults
		if "item_defaults" not in item_data:
			item_data["item_defaults"] = []

		item = frappe.get_doc(item_data)
		item.insert(ignore_permissions=True)
		return item

	def create_ecommerce_mapping(
		self,
		erpnext_item_code,
		integration_item_code,
		variant_id=None,
		has_variants=0,
	):
		"""Create an Ecommerce Item mapping for testing.

		Args:
		    erpnext_item_code: The ERPNext item code
		    integration_item_code: The Medusa product/variant ID
		    variant_id: Optional variant ID
		    has_variants: Whether this is a template product

		Returns:
		    The Ecommerce Item document name
		"""
		mapping = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": MODULE_NAME,
				"erpnext_item_code": erpnext_item_code,
				"integration_item_code": integration_item_code,
				"variant_id": variant_id or "",
				"has_variants": has_variants,
				"item_synced_on": frappe.utils.now(),
			}
		)
		mapping.insert(ignore_permissions=True)
		return mapping.name


class MockMedusaClient:
	"""Mock Medusa client for testing without actual API calls.

	This allows testing the business logic without dependency on external services.
	"""

	def __init__(self):
		self.products = {}
		self.variants = {}

	def create_product(self, payload):
		"""Mock product creation."""
		product_id = f"prod_{frappe.generate_hash(length=8)}"
		variant_id = f"variant_{frappe.generate_hash(length=8)}"

		product = {
			"id": product_id,
			"title": payload.get("title"),
			"status": payload.get("status", "draft"),
			"external_id": payload.get("external_id"),
			"variants": [
				{
					"id": variant_id,
					"title": payload.get("variants", [{}])[0].get("title", ""),
					"sku": payload.get("variants", [{}])[0].get("sku"),
				}
			],
		}

		self.products[product_id] = product
		return product

	def update_product(self, product_id, payload):
		"""Mock product update."""
		if product_id in self.products:
			self.products[product_id].update(payload)
			return self.products[product_id]
		return None

	def create_variant(self, product_id, payload):
		"""Mock variant creation."""
		variant_id = f"variant_{frappe.generate_hash(length=8)}"
		variant = {
			"id": variant_id,
			"title": payload.get("title"),
			"sku": payload.get("sku"),
			"product_id": product_id,
		}
		self.variants[variant_id] = variant
		return variant


def create_test_payload(item_code="TEST-001", **overrides):
	"""Create a test payload for retry functionality testing.

	Args:
	    item_code: The ERPNext item code to use
	    **overrides: Additional fields to override in the payload

	Returns:
	    A payload dict matching the retry structure
	"""
	payload = {
		"title": f"Test Product {item_code}",
		"description": "Test description",
		"external_id": item_code,
		"metadata": {"erpnext_item_code": item_code, "erpnext_item_group": "Consumable", "source": "erpnext"},
		"options": [{"title": "Default option", "values": ["Default variant"]}],
		"variants": [
			{
				"title": item_code,
				"sku": item_code,
				"options": {"Default option": "Default variant"},
				"prices": [{"amount": 10.0, "currency_code": "usd"}],
				"manage_inventory": False,
				"metadata": {"erpnext_item_code": item_code, "source": "erpnext"},
			}
		],
		"status": "published",
		"sales_channels": [{"id": "sc_01TEST"}],
		"sync_type": "Product Export",
	}

	# Apply overrides
	for key, value in overrides.items():
		if key in payload and isinstance(payload[key], dict) and isinstance(value, dict):
			payload[key].update(value)
		else:
			payload[key] = value

	return payload
