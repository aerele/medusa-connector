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

# Custom fields linking ERPNext masters to Medusa identity (Shopify-style).
CUSTOMER_ID_FIELD = "medusa_customer_id"
ADDRESS_ID_FIELD = "medusa_address_id"

# Sales documents (Shopify-style order identity fields).
ORDER_ID_FIELD = "medusa_order_id"
ORDER_NUMBER_FIELD = "medusa_order_number"
ORDER_STATUS_FIELD = "medusa_order_status"
ORDER_ITEM_DISCOUNT_FIELD = "medusa_item_discount"

# Admin API field expand for full order hydration (items, addresses, shipping).
DEFAULT_ORDER_FIELDS = (
	"*items,*items.variant,*items.tax_lines,"
	"*shipping_address,*billing_address,*customer,"
	"*shipping_methods,*shipping_methods.tax_lines,"
	"+currency_code,+total,+subtotal,+shipping_total,+tax_total,"
	"+discount_total,+item_total,+email,+display_id,+status,"
	"+payment_status,+fulfillment_status,+metadata"
)
