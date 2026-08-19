# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Constants for the Medusa Connector."""

from enum import Enum
from typing import Literal

MODULE_NAME = "Medusa Connector"
SETTING_DOCTYPE = "Medusa Settings"

RECEIVER_METHOD = "/api/method/medusa_connector.api.webhook.receive"

# Pagination configuration
DEFAULT_PAGE_LIMIT = 100  # Default items per page for API requests

# Webhook events registered by the Medusa Connector.
MEDUSA_WEBHOOK_EVENTS = (
	# Product
	"product.created",
	"product.updated",
	"product-variant.created",
	"product-variant.updated",
	# Order
	"order.placed",
	"order.updated",
	"order.completed",
	"order.canceled",
	# Payment
	"payment.captured",
	"payment.refunded",
	# Fulfillment
	"order.fulfillment_created",
	"order.fulfillment_canceled",
	"fulfillment.canceled",
	# Shipment / Delivery
	"order.shipment_created",
	"shipment.created",
	"delivery.created",
	# Return
	"order.return_requested",
	"order.return_received",
)


PRODUCT_SYNC_JOB_NAME = "medusa.job.sync.products"
PRODUCT_SYNC_REALTIME_KEY = "medusa.key.sync.products"

CUSTOMER_ID_FIELD = "medusa_customer_id"
ADDRESS_ID_FIELD = "medusa_address_id"

ORDER_ID_FIELD = "medusa_order_id"
ORDER_NUMBER_FIELD = "medusa_order_number"
ORDER_STATUS_FIELD = "medusa_order_status"
ORDER_ITEM_DISCOUNT_FIELD = "medusa_item_discount"

FULFILLMENT_ID_FIELD = "medusa_fulfillment_id"
REFUND_ID_FIELD = "medusa_refund_id"
RETURN_ID_FIELD = "medusa_return_id"
CLAIM_ID_FIELD = "medusa_claim_id"
EXCHANGE_ID_FIELD = "medusa_exchange_id"


class MedusaOperationStatus(Enum):
	"""Status values for Medusa connector operations."""

	SUCCESS = "success"
	SKIPPED = "skipped"
	INVALID = "invalid"
	ERROR = "error"


class IntegrationLogStatus(Enum):
	"""Status values for Ecommerce Integration Log entries."""

	SUCCESS = "Success"
	INVALID = "Invalid"
	ERROR = "Error"
	QUEUED = "Queued"
	FAILED = "Failed"


# Status mapping for backward compatibility
_RESULT_STATUS_MAP = {
	MedusaOperationStatus.SUCCESS.value: IntegrationLogStatus.SUCCESS.value,
	MedusaOperationStatus.SKIPPED.value: IntegrationLogStatus.SUCCESS.value,
	MedusaOperationStatus.INVALID.value: IntegrationLogStatus.INVALID.value,
	MedusaOperationStatus.ERROR.value: IntegrationLogStatus.ERROR.value,
}

SENSITIVE_KEYS = {
	# Personal information
	"email",
	"phone",
	"phone_number",
	"first_name",
	"last_name",
	"customer",
	"customer_id",
	# Authentication & secrets
	"password",
	"token",
	"secret",
	"authorization",
	"api_key",
	"access_token",
	"refresh_token",
	"cookie",
	"set-cookie",
	"x-api-key",
	"x-auth-token",
	"x-access-token",
	"x-medusa-signature",
	# Payment information
	"card",
	"card_number",
	"credit_card",
	"cvv",
	"cvc",
	"payments",
	"payment_data",
	"payment_collections",
	"transactions",
	# Addresses
	"address",
	"address_1",
	"address_2",
	"billing_address",
	"shipping_address",
	"postal_code",
	"zip",
}
