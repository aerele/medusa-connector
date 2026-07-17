# Copyright (c) 2026, Aerele Technologies and contributors
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
	"shipment.created",
	"delivery.created",
	# Return
	"order.return_requested",
	"order.return_received",
	"order.claim_created",
	"order.exchange_created",
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
TRANSACTION_ID_FIELD = "medusa_transaction_id"
ORDER_LINE_ID_FIELD = "medusa_order_line_id"


class MedusaOperationStatus(Enum):
	"""Status values for Medusa connector operations."""

	SUCCESS = "success"
	SKIPPED = "skipped"
	ERROR = "error"


class IntegrationLogStatus(Enum):
	"""Status values for Ecommerce Integration Log entries."""

	SUCCESS = "Success"
	ERROR = "Error"
	QUEUED = "Queued"
	FAILED = "Failed"


# Status mapping for backward compatibility
_RESULT_STATUS_MAP = {
	MedusaOperationStatus.SUCCESS.value: IntegrationLogStatus.SUCCESS.value,
	MedusaOperationStatus.SKIPPED.value: IntegrationLogStatus.SUCCESS.value,
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


# Admin API field expand for full order hydration (items, addresses, shipping, fulfillments).
DEFAULT_ORDER_FIELDS = (
	"*items,*items.variant,*items.tax_lines,*items.adjustments,"
	"*shipping_address,*billing_address,*customer,"
	"*shipping_methods,*shipping_methods.tax_lines,*shipping_methods.adjustments,"
	"*fulfillments,*fulfillments.items,*fulfillments.labels,"
	"*payment_collections,*payment_collections.payments,*payment_collections.payments.refunds,"
	"*returns,*returns.items,*returns.shipping_methods,*returns.transactions,"
	"*claims,*claims.return,*claims.additional_items,*claims.transactions,"
	"*exchanges,*exchanges.return,*exchanges.additional_items,*exchanges.transactions,"
	"*transactions,*summary,"
	"+currency_code,+total,+subtotal,+shipping_total,+tax_total,"
	"+discount_total,+discount_tax_total,+item_total,+item_subtotal,+item_tax_total,"
	"+shipping_subtotal,+shipping_tax_total,+original_total,+original_tax_total,"
	"+gift_card_total,+gift_card_tax_total,+credit_line_total,+email,+display_id,+status,"
	"+payment_status,+fulfillment_status,+metadata"
)

# Order fetch focused on fulfillments (DN sync).
DEFAULT_ORDER_FULFILLMENT_FIELDS = (
	"id,display_id,status,payment_status,fulfillment_status,created_at,"
	"*items,*items.variant,"
	"*fulfillments,*fulfillments.items,*fulfillments.labels"
)

# Admin payment retrieve: collection + linked order (order_payment_collection link).
# Webhook bodies only include payment id; order is resolved via this expand.
DEFAULT_PAYMENT_FIELDS = (
	"id,amount,currency_code,captured_at,canceled_at,"
	"payment_collection_id,payment_session_id,provider_id,"
	"*captures,*refunds,"
	"*payment_collection,*payment_collection.order"
)
