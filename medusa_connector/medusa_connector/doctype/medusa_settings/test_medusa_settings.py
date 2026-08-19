# Copyright (c) 2026, Aerele and Contributors
# See license.txt

from unittest.mock import patch

import frappe
import responses
from frappe.tests import IntegrationTestCase

from medusa_connector.constants import SETTING_DOCTYPE


class TestMedusaSettings(IntegrationTestCase):
	"""Test essential Medusa Settings functionality."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings = frappe.get_doc(SETTING_DOCTYPE)

	@responses.activate
	def test_connection_verification_success(self):
		"""Verify valid Medusa credentials establish a connection."""
		responses.add(
			responses.GET,
			"https://medusa.example.com/admin/regions",
			status=200,
			json={"regions": [{"id": "region_123"}], "count": 1},
		)

		responses.add(
			responses.GET,
			"https://medusa.example.com/admin/stores",
			status=200,
			json={
				"stores": [
					{
						"id": "store_123",
						"name": "Test Store",
						"default_sales_channel_id": "channel_123",
						"default_region_id": "region_123",
						"default_location_id": "loc_123",
						"supported_currencies": [
							{
								"currency_code": "inr",
								"is_default": True,
							},
							{
								"currency_code": "usd",
								"is_default": False,
							},
						],
					}
				],
				"count": 1,
			},
		)

		self.settings.medusa_base_url = "https://medusa.example.com"
		self.settings.admin_api_key = "test-api-key"
		self.settings.enabled = 1

		self.settings._verify_connection()

		self.assertEqual(
			self.settings.connection_status,
			"Connected",
		)
		self.assertEqual(
			self.settings.medusa_store_id,
			"store_123",
		)
		self.assertEqual(
			self.settings.default_currency,
			"inr",
		)

	@responses.activate
	def test_connection_verification_auth_failure(self):
		"""Verify invalid Medusa credentials are rejected."""
		responses.add(
			responses.GET,
			"https://medusa.example.com/admin/regions",
			status=401,
			json={"message": "Unauthorized"},
		)

		self.settings.medusa_base_url = "https://medusa.example.com"
		self.settings.admin_api_key = "invalid-key"
		self.settings.enabled = 1

		with self.assertRaises(frappe.ValidationError) as context:
			self.settings._verify_connection()

		self.assertIn(
			"Admin API Key",
			str(context.exception),
		)

	@responses.activate
	def test_fetch_medusa_locations(self):
		"""Verify Medusa locations are loaded into warehouse mapping."""
		responses.add(
			responses.GET,
			"https://medusa.example.com/admin/stock-locations",
			status=200,
			json={
				"stock_locations": [
					{"id": "loc1", "name": "Warehouse 1"},
					{"id": "loc2", "name": "Warehouse 2"},
				],
				"count": 2,
			},
		)

		self.settings.medusa_base_url = "https://medusa.example.com"
		self.settings.admin_api_key = "test-key"
		self.settings.enabled = 1
		self.settings.warehouse_mapping = []

		self.settings.fetch_medusa_locations()

		self.assertEqual(
			len(self.settings.warehouse_mapping),
			2,
		)
		self.assertEqual(
			self.settings.warehouse_mapping[0].medusa_location_id,
			"loc1",
		)
		self.assertEqual(
			self.settings.warehouse_mapping[0].medusa_location_name,
			"Warehouse 1",
		)
		self.assertEqual(
			self.settings.warehouse_mapping[1].medusa_location_id,
			"loc2",
		)
		self.assertEqual(
			self.settings.warehouse_mapping[1].medusa_location_name,
			"Warehouse 2",
		)

	def test_seed_warehouse_mapping(self):
		"""Verify default warehouse mapping is created when mapping is empty."""
		self.settings.update_erpnext_stock_levels_to_medusa = 1
		self.settings.warehouse = "Warehouse - _TC"
		self.settings.default_location_id = "default_loc"
		self.settings.warehouse_mapping = []

		self.settings._seed_warehouse_mapping_if_needed()

		self.assertEqual(
			len(self.settings.warehouse_mapping),
			1,
		)
		self.assertEqual(
			self.settings.warehouse_mapping[0].medusa_location_id,
			"default_loc",
		)
		self.assertEqual(
			self.settings.warehouse_mapping[0].erpnext_warehouse,
			"Warehouse - _TC",
		)
		self.assertEqual(
			self.settings.warehouse_mapping[0].enabled,
			1,
		)

	def test_warehouse_mapping_validation(self):
		"""Verify duplicate warehouse and location mappings are rejected."""
		self.settings.update_erpnext_stock_levels_to_medusa = 1

		# Duplicate ERPNext warehouse.
		self.settings.set("warehouse_mapping", [])

		self.settings.append(
			"warehouse_mapping",
			{
				"medusa_location_id": "loc1",
				"erpnext_warehouse": "Warehouse - _TC",
				"enabled": 1,
			},
		)
		self.settings.append(
			"warehouse_mapping",
			{
				"medusa_location_id": "loc2",
				"erpnext_warehouse": "Warehouse - _TC",
				"enabled": 1,
			},
		)

		with self.assertRaises(frappe.ValidationError):
			self.settings._validate_inventory_settings()

		# Duplicate Medusa location.
		self.settings.set("warehouse_mapping", [])

		self.settings.append(
			"warehouse_mapping",
			{
				"medusa_location_id": "loc1",
				"erpnext_warehouse": "Warehouse - _TC",
				"enabled": 1,
			},
		)
		self.settings.append(
			"warehouse_mapping",
			{
				"medusa_location_id": "loc1",
				"erpnext_warehouse": "Warehouse 2 - _TC",
				"enabled": 1,
			},
		)

		with self.assertRaises(frappe.ValidationError):
			self.settings._validate_inventory_settings()

	@patch(
		"medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.secrets.token_urlsafe",
		return_value="new-secret-token",
	)
	def test_webhook_secret_regeneration(self, mock_token):
		"""Verify webhook secret is regenerated and webhook state is reset."""
		from medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings import (
			regenerate_webhook_secret,
		)

		self.settings.webhook_secret = "old-secret"
		self.settings.webhook_plugin_status = "Installed"
		self.settings.last_webhook_sync = "2026-08-10 12:00:00"
		self.settings.last_webhook_sync_message = "Webhooks synced"
		self.settings.set(
			"webhook_subscriptions",
			[
				{
					"medusa_event": "product.created",
					"registration_status": "Registered",
				}
			],
		)

		with (
			patch(
				"frappe.get_single",
				return_value=self.settings,
			),
			patch.object(self.settings, "save"),
		):
			result = regenerate_webhook_secret()

		self.assertEqual(
			result,
			"new-secret-token",
		)
		self.assertEqual(
			self.settings.webhook_plugin_status,
			"Unknown",
		)
		self.assertIsNone(
			self.settings.last_webhook_sync,
		)
		self.assertEqual(
			self.settings.last_webhook_sync_message,
			"Webhook secret changed. Please sync webhooks.",
		)
		self.assertEqual(
			self.settings.webhook_subscriptions,
			[],
		)

		mock_token.assert_called_once_with(32)
