# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Order domain: Medusa → ERPNext order lifecycle sync and webhooks.

- ``order.sync`` — SO create + full lifecycle (SI/PE/DN/cancel); bulk Sync Orders
- ``order.invoice`` — optional Sales Invoice on payment
- ``order.fulfillment`` — Delivery Note per Medusa fulfillment
- ``order.webhook`` — Medusa order/payment/fulfillment event handlers

Customer helpers: import from ``medusa_connector.customer.sync`` directly.
"""
