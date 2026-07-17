# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Customer domain: order-time Medusa → ERPNext Customer / Address / Contact.

Uses ``ecommerce_core.controllers.customer.EcommerceCustomer``. No bulk import
or instant customer-webhook create — see ``sync.ensure_order_customer``.
"""
