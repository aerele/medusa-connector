# Copyright (c) 2026, Aerele Technologies and contributors

# For license information, please see license.txt

"""Tests for Medusa webhook API operations."""

from __future__ import annotations

import responses
from frappe.tests import UnitTestCase

from medusa_connector.medusa.client import MedusaClient


class TestMedusaClient(UnitTestCase):
	"""Test Medusa webhook API operations."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.client = MedusaClient(settings=cls.get_settings())

	@staticmethod
	def get_settings():
		from unittest.mock import MagicMock

		settings = MagicMock()
		settings.enabled = 1
		settings.medusa_base_url = "https://medusa.example.com"
		settings.get_password.return_value = "test-api-key"
		return settings

	def setUp(self):
		self.responses = responses.RequestsMock()
		self.responses.start()

		self.addCleanup(self.responses.stop)
		self.addCleanup(self.responses.reset)

	def test_list_webhooks(self):
		"""requirement: existing webhooks are returned from Medusa."""
		self.responses.add(
			responses.GET,
			"https://medusa.example.com/admin/webhooks",
			status=200,
			json={
				"subscriptions": [
					{
						"id": "wh_1",
						"event_type": "product.created",
					},
					{
						"id": "wh_2",
						"event_type": "order.placed",
					},
				],
				"count": 2,
			},
		)

		webhooks = self.client.list_webhooks()

		self.assertEqual(
			[webhook["id"] for webhook in webhooks],
			["wh_1", "wh_2"],
		)

	def test_create_webhook(self):
		"""requirement: webhook is created with the expected event and target URL."""
		target_url = "https://erp.example.com/api/method/medusa_connector.api.webhook.receive"

		self.responses.add(
			responses.POST,
			"https://medusa.example.com/admin/webhooks",
			status=200,
			json={
				"webhook": {
					"id": "wh_new",
					"event_type": "product.created",
					"target_url": target_url,
					"active": True,
				},
			},
		)

		result = self.client.create_webhook(
			"product.created",
			target_url,
			active=True,
		)

		request = self.responses.calls[-1].request

		self.assertEqual(request.method, "POST")
		self.assertEqual(result["id"], "wh_new")

	def test_create_webhook_does_not_include_secret_in_url(self):
		"""requirement: webhook target URL must not contain the webhook secret."""
		target_url = "https://erp.example.com/api/method/medusa_connector.api.webhook.receive"

		self.responses.add(
			responses.POST,
			"https://medusa.example.com/admin/webhooks",
			status=200,
			json={
				"webhook": {
					"id": "wh_new",
					"event_type": "product.created",
					"target_url": target_url,
					"active": True,
				},
			},
		)

		self.client.create_webhook(
			"product.created",
			target_url,
			active=True,
		)

		request = self.responses.calls[-1].request

		self.assertNotIn("secret", request.body.decode().lower())

	def test_delete_webhook(self):
		"""requirement: existing webhook is deleted using the DELETE method."""
		self.responses.add(
			responses.DELETE,
			"https://medusa.example.com/admin/webhooks/wh_1",
			status=204,
		)

		self.client.delete_webhook("wh_1")

		request = self.responses.calls[-1].request

		self.assertEqual(request.method, "DELETE")
		self.assertEqual(
			request.url,
			"https://medusa.example.com/admin/webhooks/wh_1",
		)
