# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Constants for the Medusa Connector."""

MODULE_NAME = "Medusa Connector"
SETTING_DOCTYPE = "Medusa Settings"

RECEIVER_METHOD = "/api/method/medusa_connector.api.webhook.receive"

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


ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"
INTEGRATION_LOG_DOCTYPE = "Ecommerce Integration Log"

PRODUCT_SYNC_JOB_NAME = "medusa.job.sync.products"
PRODUCT_SYNC_REALTIME_KEY = "medusa.key.sync.products"

DEFAULT_ITEM_GROUP = "All Item Groups"
DEFAULT_STOCK_UOM = "Nos"
DEFAULT_CUSTOMER_GROUP = "Individual"

CUSTOMER_ID_FIELD = "medusa_customer_id"
ADDRESS_ID_FIELD = "medusa_address_id"

ORDER_ID_FIELD = "medusa_order_id"
ORDER_NUMBER_FIELD = "medusa_order_number"
ORDER_STATUS_FIELD = "medusa_order_status"
ORDER_ITEM_DISCOUNT_FIELD = "medusa_item_discount"

FULFILLMENT_ID_FIELD = "medusa_fulfillment_id"
REFUND_ID_FIELD = "medusa_refund_id"

DEFAULT_ORDER_FIELDS = (
	"*items,*items.variant,*items.tax_lines,"
	"*shipping_address,*billing_address,*customer,"
	"*shipping_methods,*shipping_methods.tax_lines,"
	"*fulfillments,*fulfillments.items,*fulfillments.labels,"
	"*payment_collections,*payment_collections.payments,"
	"+currency_code,+total,+subtotal,+shipping_total,+tax_total,"
	"+discount_total,+item_total,+email,+display_id,+status,"
	"+payment_status,+fulfillment_status,+metadata"
)

DEFAULT_ORDER_FULFILLMENT_FIELDS = (
	"id,display_id,status,payment_status,fulfillment_status,created_at,"
	"*items,*items.variant,"
	"*fulfillments,*fulfillments.items,*fulfillments.labels"
)

DEFAULT_PAYMENT_FIELDS = (
	"id,amount,currency_code,captured_at,canceled_at,"
	"payment_collection_id,payment_session_id,provider_id,"
	"*captures,*refunds,"
	"*payment_collection,*payment_collection.order"
)

_RESULT_STATUS_MAP = {
	"success": "Success",
	"skipped": "Success",
	"invalid": "Invalid",
	"error": "Error",
}
