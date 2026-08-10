# Copyright (c) 2026, Aerele and contributors

# For license information, please see license.txt

"""Tests for Medusa webhook synchronization."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase
from frappe.utils import now_datetime

from medusa_connector.api.webhook import authenticate
from medusa_connector.constants import MEDUSA_WEBHOOK_EVENTS
from medusa_connector.medusa.webhook_sync import (
	STATUS_FAILED,
	STATUS_REGISTERED,
	WebhookSyncService,
)


class TestWebhookSyncService(UnitTestCase):
	"""Test real-world Medusa webhook registration scenarios."""

	def setUp(self):
		self.settings = MagicMock()
		self.settings.get_password.return_value = "test-secret"

	@patch("medusa_connector.medusa.webhook_sync.receiver_base_url")
	def test_sync_webhooks_registers_all_required_events(
		self,
		mock_receiver_base_url,
	):
		"""requirement: all required Medusa webhooks are registered successfully."""
		target_url = "https://erp.example.com/api/method/medusa_connector.api.webhook.receive"

		mock_receiver_base_url.return_value = target_url

		client = MagicMock()
		client.webhook_plugin_installed.return_value = True
		client.list_webhooks.return_value = []

		service = WebhookSyncService(settings=self.settings)

		created_webhooks = {
			event: {
				"id": f"wh_{index}",
				"event_type": event,
				"target_url": target_url,
			}
			for index, event in enumerate(
				MEDUSA_WEBHOOK_EVENTS,
				start=1,
			)
		}

		def create_webhook(event, target_url, active=True):
			return created_webhooks[event]

		client.create_webhook.side_effect = create_webhook

		with (
			patch(
				"medusa_connector.medusa.webhook_sync.MedusaClient",
				return_value=client,
			),
			patch(
				"medusa_connector.medusa.webhook_sync.frappe.get_single",
				return_value=self.settings,
			),
			patch(
				"medusa_connector.medusa.webhook_sync.now_datetime",
				return_value="2026-07-29 18:00:00",
			),
		):
			result = service.sync_webhooks()

		self.assertEqual(result["status"], "Installed")
		self.assertEqual(
			result["count"],
			len(MEDUSA_WEBHOOK_EVENTS),
		)
		self.assertEqual(
			result["created"],
			len(MEDUSA_WEBHOOK_EVENTS),
		)

		self.assertEqual(
			client.create_webhook.call_count,
			len(MEDUSA_WEBHOOK_EVENTS),
		)

		for event in MEDUSA_WEBHOOK_EVENTS:
			self.assertTrue(any(call.args[0] == event for call in client.create_webhook.call_args_list))

		for call in client.create_webhook.call_args_list:
			self.assertEqual(call.args[1], target_url)

	def test_sync_webhooks_marks_failed_registration(self):
		"""requirement: failed webhook registration is marked as Failed."""
		service = WebhookSyncService(settings=self.settings)

		client = MagicMock()

		def create_webhook(event, target_url, active=True):
			if event == "order.placed":
				raise Exception("Medusa API failed")

			return {
				"id": f"wh_{event}",
				"event_type": event,
				"target_url": target_url,
			}

		client.create_webhook.side_effect = create_webhook

		with patch(
			"medusa_connector.medusa.webhook_sync.receiver_base_url",
			return_value=("https://erp.example.com/api/method/medusa_connector.api.webhook.receive"),
		):
			rows = [service._register_webhook(client, event) for event in MEDUSA_WEBHOOK_EVENTS]

		failed_rows = [row for row in rows if row["registration_status"] == STATUS_FAILED]

		registered_rows = [row for row in rows if row["registration_status"] == STATUS_REGISTERED]

		self.assertEqual(len(failed_rows), 1)
		self.assertEqual(
			failed_rows[0]["medusa_event"],
			"order.placed",
		)
		self.assertEqual(
			failed_rows[0]["last_error"],
			"Medusa API failed",
		)

		self.assertEqual(
			len(registered_rows),
			len(MEDUSA_WEBHOOK_EVENTS) - 1,
		)

	def test_sync_webhooks_handles_missing_plugin(self):
		"""requirement: missing webhook plugin marks sync as Not Installed."""
		settings = MagicMock()
		settings.get_password.return_value = "test-secret"

		with (
			patch(
				"medusa_connector.medusa.webhook_sync.receiver_base_url",
				return_value=("https://erp.example.com/api/method/medusa_connector.api.webhook.receive"),
			),
			patch(
				"medusa_connector.medusa.webhook_sync.frappe.get_single",
				return_value=settings,
			),
			patch(
				"medusa_connector.medusa.webhook_sync.MedusaClient",
			) as mock_client_class,
		):
			client = mock_client_class.return_value
			client.webhook_plugin_installed.return_value = False

			service = WebhookSyncService(settings=settings)
			result = service.sync_webhooks()

		self.assertEqual(
			result["status"],
			"Not Installed",
		)

		self.assertEqual(
			result["count"],
			len(MEDUSA_WEBHOOK_EVENTS),
		)

		client.webhook_plugin_installed.assert_called_once()


class TestWebhookAuthentication(UnitTestCase):
	def test_valid_signature(self):
		secret = "test-secret"
		timestamp = str(int(now_datetime().timestamp()))

		body = json.dumps(
			{
				"event": "product.created",
				"data": {
					"id": "prod_123",
				},
			}
		)

		signed_content = f"{timestamp}.{body}"

		signature = hmac.new(
			secret.encode(),
			signed_content.encode(),
			hashlib.sha256,
		).hexdigest()

		settings = MagicMock()
		settings.get_password.return_value = secret

		headers = {
			"X-Medusa-Signature": f"sha256={signature}",
			"X-Medusa-Timestamp": timestamp,
		}

		self.assertTrue(
			authenticate(
				settings,
				body.encode(),
				headers,
			)
		)

	def test_invalid_signature(self):
		secret = "test-secret"
		timestamp = str(int(now_datetime().timestamp()))

		body = json.dumps(
			{
				"event": "product.created",
				"data": {
					"id": "prod_123",
				},
			}
		)

		settings = MagicMock()
		settings.get_password.return_value = secret
		headers = {
			"X-Medusa-Signature": "sha256=invalid",
			"X-Medusa-Timestamp": timestamp,
		}

		self.assertFalse(
			authenticate(
				settings,
				body.encode(),
				headers,
			)
		)

	def test_expired_timestamp(self):
		secret = "test-secret"
		timestamp = str(int(now_datetime().timestamp()) - 600)

		body = b'{"event":"product.created","data":{"id":"prod_123"}}'

		settings = MagicMock()
		settings.get_password.return_value = secret

		headers = {
			"X-Medusa-Signature": "sha256=anything",
			"X-Medusa-Timestamp": timestamp,
		}

		self.assertFalse(
			authenticate(
				settings,
				body,
				headers,
			)
		)
