# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Central constants for the Medusa Connector (standalone app)."""

# Must match modules.txt / Module Def name (Ecommerce Item.integration Link).
MODULE_NAME = "Medusa Connector"
SETTING_DOCTYPE = "Medusa Settings"

# Ecommerce Core doctypes (shared across integrations).
ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"
INTEGRATION_LOG_DOCTYPE = "Ecommerce Integration Log"

# Realtime / RQ job names for the Sync Products page.
PRODUCT_SYNC_JOB_NAME = "medusa.job.sync.products"
PRODUCT_SYNC_REALTIME_KEY = "medusa.key.sync.products"

# Default ERPNext values used when Settings leave them blank.
DEFAULT_ITEM_GROUP = "All Item Groups"
DEFAULT_STOCK_UOM = "Nos"
DEFAULT_CUSTOMER_GROUP = "Individual"

# Custom fields linking ERPNext masters to Medusa identity.
CUSTOMER_ID_FIELD = "medusa_customer_id"
ADDRESS_ID_FIELD = "medusa_address_id"

# Sales document order identity custom fields.
ORDER_ID_FIELD = "medusa_order_id"
ORDER_NUMBER_FIELD = "medusa_order_number"
ORDER_STATUS_FIELD = "medusa_order_status"
ORDER_ITEM_DISCOUNT_FIELD = "medusa_item_discount"
# One Delivery Note per Medusa fulfillment (Shopify-style ship-unit key).
FULFILLMENT_ID_FIELD = "medusa_fulfillment_id"
# Idempotency key linking a refunded Medusa payment to the reversed ERPNext
# documents (Payment Entry + Sales Invoice). Stamped when a refund is applied
# so a re-delivered ``payment.refunded`` webhook does not double-process.
REFUND_ID_FIELD = "medusa_refund_id"

# Admin API field expand for full order hydration (items, addresses, shipping, fulfillments).
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
