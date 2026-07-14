# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Medusa → ERPNext customer helpers (Shopify / Ecommerce Core pattern).

Customer create/update runs **only during order synchronisation** — not via
standalone customer webhooks or a bulk import job.

Uses ``ecommerce_core.controllers.customer.EcommerceCustomer`` for Customer /
Address / Contact creation (same base class as Shopify).
"""

from __future__ import annotations

from typing import Any

import frappe
from ecommerce_core.controllers.customer import EcommerceCustomer
from ecommerce_core.utils.address_mapping import get_country_name
from frappe import _
from frappe.utils import cstr, validate_phone_number
from frappe.utils.nestedset import get_root_of

from medusa_connector.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	DEFAULT_CUSTOMER_GROUP,
	MODULE_NAME,
	SETTING_DOCTYPE,
)
from medusa_connector.medusa.customer import CustomerService


class MedusaCustomer(EcommerceCustomer):
	"""Shopify-style customer adapter for Medusa order payloads."""

	def __init__(self, customer_id: str, settings=None):
		self.setting = settings or frappe.get_doc(SETTING_DOCTYPE)
		super().__init__(cstr(customer_id), CUSTOMER_ID_FIELD, MODULE_NAME)

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer + addresses + contact when not already synced."""
		customer_name = _customer_display_name(customer)
		customer_group = self.setting.customer_group or DEFAULT_CUSTOMER_GROUP
		if not frappe.db.exists("Customer Group", customer_group):
			customer_group = (
				DEFAULT_CUSTOMER_GROUP
				if frappe.db.exists("Customer Group", DEFAULT_CUSTOMER_GROUP)
				else get_root_of("Customer Group")
			)

		super().sync_customer(customer_name, customer_group)

		billing_address = (
			customer.get("billing_address")
			or customer.get("default_billing_address")
			or _default_profile_address(customer, billing=True)
		)
		shipping_address = (
			customer.get("shipping_address")
			or customer.get("default_shipping_address")
			or _default_profile_address(customer, billing=False)
		)

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=customer.get("email")
			)
		if shipping_address:
			# Avoid duplicate when same Medusa address id is both billing and shipping.
			if not (
				isinstance(billing_address, dict)
				and billing_address.get("id")
				and shipping_address.get("id")
				and billing_address.get("id") == shipping_address.get("id")
			):
				self.create_customer_address(
					customer_name,
					shipping_address,
					address_type="Shipping",
					email=customer.get("email"),
				)

		self.create_customer_contact(customer)

	def create_customer_address(
		self,
		customer_name,
		medusa_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		address_fields = _map_address_fields(medusa_address, customer_name, address_type, email)
		super().create_customer_address(address_fields)

	def update_existing_addresses(self, customer: dict[str, Any]) -> None:
		"""Update billing/shipping addresses on an already-synced customer (Shopify)."""
		billing_address = (
			customer.get("billing_address")
			or customer.get("default_billing_address")
			or _default_profile_address(customer, billing=True)
		)
		shipping_address = (
			customer.get("shipping_address")
			or customer.get("default_shipping_address")
			or _default_profile_address(customer, billing=False)
		)
		customer_name = _customer_display_name(customer)
		email = customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		medusa_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		old_address = self.get_customer_address_doc(address_type)

		if not old_address:
			self.create_customer_address(customer_name, medusa_address, address_type, email)
			return

		exclude_in_update = ["address_title", "address_type"]
		new_values = _map_address_fields(medusa_address, customer_name, address_type, email)
		old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
		old_address.flags.ignore_mandatory = True
		old_address.save(ignore_permissions=True)

	def create_customer_contact(self, medusa_customer: dict[str, Any]) -> None:
		first = cstr(medusa_customer.get("first_name")).strip()
		email = cstr(medusa_customer.get("email")).strip()
		if not (first or email):
			return
		if not first and email:
			first = email.split("@")[0]

		contact_fields: dict[str, Any] = {
			"status": "Passive",
			"first_name": first,
			"last_name": cstr(medusa_customer.get("last_name")).strip() or None,
		}
		if email:
			contact_fields["email_ids"] = [{"email_id": email, "is_primary": True}]

		phone_no = medusa_customer.get("phone") or (
			(medusa_customer.get("billing_address") or {}).get("phone")
		)
		if phone_no and validate_phone_number(cstr(phone_no), throw=False):
			contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

		super().create_customer_contact(contact_fields)


def resolve_order_customer(order: dict, *, settings=None, service: CustomerService | None = None) -> str:
	"""Resolve ERPNext Customer for a Medusa order (create if missing).

	Shopify flow:
	  if not synced → create customer + address + contact
	  else → update addresses
	"""
	settings = settings or frappe.get_cached_doc(SETTING_DOCTYPE)
	if not order:
		return _default_customer_or_throw(settings)

	customer_id = cstr(order.get("customer_id") or (order.get("customer") or {}).get("id") or "")
	if not customer_id:
		return _default_customer_or_throw(settings)

	payload = _payload_from_order(order, customer_id=customer_id)

	# Hydrate thin customer blobs so we have name/email/addresses when possible.
	if not _looks_hydrated(payload):
		service = service or CustomerService()
		try:
			fetched = service.get_customer(customer_id)
			if fetched:
				payload = _merge_customer_payload(fetched, order)
				payload["id"] = customer_id
		except Exception as exc:
			frappe.logger("medusa_connector").warning(
				f"Could not hydrate Medusa customer {customer_id} for order: {exc}"
			)

	medusa_customer = MedusaCustomer(customer_id, settings=settings)
	if not medusa_customer.is_synced():
		medusa_customer.sync_customer(payload)
	else:
		medusa_customer.update_existing_addresses(payload)

	return medusa_customer.get_customer_doc().name


def ensure_order_customer(order: dict, *, settings=None) -> str:
	"""Alias used by order pipeline (same as Shopify pre-SO customer step)."""
	return resolve_order_customer(order, settings=settings)


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _payload_from_order(order: dict, *, customer_id: str) -> dict[str, Any]:
	payload: dict[str, Any] = {"id": customer_id}
	nested = order.get("customer") if isinstance(order.get("customer"), dict) else {}
	payload.update({k: v for k, v in (nested or {}).items() if v is not None})
	if order.get("email") and not payload.get("email"):
		payload["email"] = order.get("email")
	if order.get("billing_address"):
		payload["billing_address"] = order["billing_address"]
	if order.get("shipping_address"):
		payload["shipping_address"] = order["shipping_address"]
	return payload


def _merge_customer_payload(customer: dict, order: dict | None = None) -> dict[str, Any]:
	payload = dict(customer or {})
	if order:
		if order.get("billing_address") and not payload.get("billing_address"):
			payload["billing_address"] = order["billing_address"]
		if order.get("shipping_address") and not payload.get("shipping_address"):
			payload["shipping_address"] = order["shipping_address"]
		if order.get("email") and not payload.get("email"):
			payload["email"] = order.get("email")
	return payload


def _looks_hydrated(payload: dict) -> bool:
	if payload.get("email") or payload.get("first_name") or payload.get("last_name"):
		return True
	if payload.get("company_name") or payload.get("company"):
		return True
	if payload.get("addresses") or payload.get("billing_address") or payload.get("shipping_address"):
		return True
	return False


def _default_profile_address(customer: dict, *, billing: bool) -> dict | None:
	addresses = customer.get("addresses") or []
	if not isinstance(addresses, list):
		return None
	for addr in addresses:
		if not isinstance(addr, dict):
			continue
		if billing and addr.get("is_default_billing"):
			return addr
		if not billing and addr.get("is_default_shipping"):
			return addr
	return addresses[0] if addresses else None


def _customer_display_name(payload: dict) -> str:
	company = cstr(payload.get("company_name") or payload.get("company")).strip()
	if company:
		return company[:140]
	first = cstr(payload.get("first_name")).strip()
	last = cstr(payload.get("last_name")).strip()
	name = f"{first} {last}".strip()
	if name:
		return name[:140]
	email = cstr(payload.get("email")).strip()
	if email:
		return email[:140]
	return cstr(payload.get("id") or "Medusa Customer")[:140]


def _default_customer_or_throw(settings) -> str:
	default = settings.get("default_customer")
	if default and frappe.db.exists("Customer", default):
		return default
	frappe.throw(
		_(
			"Medusa order has no customer_id and Default Customer is not set on Medusa Settings. "
			"Set a Default Customer for guest checkouts."
		),
		title=_("Customer Required"),
	)


def _map_address_fields(
	medusa_address: dict,
	customer_name: str,
	address_type: str,
	email: str | None,
) -> dict[str, Any]:
	"""Map Medusa address dict → ERPNext Address fields (Shopify-style)."""
	country_code = medusa_address.get("country_code") or medusa_address.get("country")
	country = get_country_name(cstr(country_code).upper() if country_code else None)
	if not country and country_code and frappe.db.exists("Country", cstr(country_code)):
		country = cstr(country_code)
	if not country:
		country = frappe.db.get_default("country")

	line1 = (
		medusa_address.get("address_1")
		or medusa_address.get("address_line1")
		or medusa_address.get("address1")
		or "Address 1"
	)
	line2 = (
		medusa_address.get("address_2")
		or medusa_address.get("address_line2")
		or medusa_address.get("address2")
	)

	fields: dict[str, Any] = {
		"address_title": customer_name,
		"address_type": address_type,
		"address_line1": cstr(line1)[:140],
		"address_line2": cstr(line2)[:140] if line2 else None,
		"city": cstr(medusa_address.get("city") or "Unknown")[:100],
		"state": cstr(medusa_address.get("province") or medusa_address.get("state") or "")[:100],
		"pincode": cstr(
			medusa_address.get("postal_code")
			or medusa_address.get("zip")
			or medusa_address.get("pincode")
			or ""
		)[:20],
		"country": country,
		"email_id": email or medusa_address.get("email"),
	}
	if medusa_address.get("id"):
		fields[ADDRESS_ID_FIELD] = cstr(medusa_address["id"])

	phone = medusa_address.get("phone")
	if phone and validate_phone_number(cstr(phone), throw=False):
		fields["phone"] = cstr(phone)[:30]

	return fields
