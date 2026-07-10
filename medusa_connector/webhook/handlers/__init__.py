# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Handler package.

Importing this package imports every handler module, whose ``@register``
decorators populate the central registry. To add a new event handler, create a
module here and decorate its class — it is picked up automatically.
"""

from importlib import import_module

from medusa_connector.webhook.handlers import (
	customer,
	fulfillment,
	inventory,
	order,
	payment,
	price,
	product,
	product_category,
	product_collection,
	product_variant,
	region,
	sales_channel,
	shipment,
	stock_location,
)

# ``return`` is a valid module filename but a Python keyword, so import it dynamically.
import_module("medusa_connector.webhook.handlers.return")
