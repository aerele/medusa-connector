# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Order domain: Medusa → ERPNext Sales Order sync and webhooks.

- ``order.sync`` — create/cancel Sales Order, old-order bulk entry point
- ``order.invoice`` — optional Sales Invoice on payment
- ``order.webhook`` — Medusa order/payment event handlers

Customer helpers: import from ``medusa_connector.customer.sync`` directly.
"""
