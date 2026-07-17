# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Medusa Admin payment API (retrieve + resolve order).

Medusa v2 payment webhooks (``payment.captured``, ``medusa_connector/medusa/order.pye``) deliver a
thin payload ``{"id": "pay_..."}`` without ``order_id``. The Admin API models
payments as:

  Payment → payment_collection_id → PaymentCollection ⇄ Order
  (module link ``order_payment_collection``; PaymentCollection field alias ``order``)

Resolve the order via::

  GET /admin/payments/{id}?fields=...,*payment_collection,*payment_collection.order
"""

from __future__ import annotations

from frappe.utils import cstr

from medusa_connector.constants import DEFAULT_PAYMENT_FIELDS
from medusa_connector.medusa.client import MedusaClient


class PaymentService:
	"""Fetch Medusa payments and resolve the linked order id."""

	def __init__(self, client: MedusaClient | None = None) -> None:
		self.client = client or MedusaClient()

	def get_payment(self, payment_id: str, *, fields: str | None = DEFAULT_PAYMENT_FIELDS) -> dict:
		"""``GET /admin/payments/{id}`` — unwrap ``payment`` key."""
		if not payment_id:
			return {}
		params = {"fields": fields} if fields else None
		response = self.client.execute_rest("GET", f"/admin/payments/{payment_id}", params=params)
		if not isinstance(response, dict):
			return {}
		payment = response.get("payment", response)
		return payment if isinstance(payment, dict) else {}

	def resolve_order_id(self, payment_or_payload: dict | None, *, payment_id: str | None = None) -> str:
		"""Return Medusa order id for a payment payload or payment id.

		Resolution order (first non-empty wins):

		1. Explicit ``order_id`` on the payload (rare / future-proof).
		2. Nested ``order.id`` already present on the payload.
		3. Nested ``payment_collection.order.id`` after Admin hydrate.
		4. Admin ``GET /admin/payments/{id}`` with payment_collection.order expand.
		"""
		entity = payment_or_payload if isinstance(payment_or_payload, dict) else {}
		pay_id = cstr(payment_id or entity.get("id") or "")

		order_id = _order_id_from_entity(entity)
		if order_id:
			return order_id

		if not pay_id:
			return ""

		payment = self.get_payment(pay_id)
		return _order_id_from_entity(payment)


def _order_id_from_entity(entity: dict) -> str:
	"""Extract order id from a payment (or payment-like) dict without network I/O."""
	if not entity:
		return ""

	# Direct field if ever present on webhook/API shapes
	direct = cstr(entity.get("order_id") or "")
	if direct:
		return direct

	# Nested order object
	order = entity.get("order")
	if isinstance(order, dict):
		oid = cstr(order.get("id") or "")
		if oid:
			return oid

	# Payment → payment_collection → order (Medusa module link expand)
	collection = entity.get("payment_collection")
	if isinstance(collection, dict):
		col_order = collection.get("order")
		if isinstance(col_order, dict):
			oid = cstr(col_order.get("id") or "")
			if oid:
				return oid
		# Some payloads may flatten the link as order_id on the collection
		oid = cstr(collection.get("order_id") or "")
		if oid:
			return oid

	return ""
