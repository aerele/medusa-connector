# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Handler package.

Importing this package imports every handler module, whose ``@register``
decorators populate the central registry. To add a new event handler, create a
module here and decorate its class — it is picked up automatically.
"""

from medusa_connector.webhook.handlers import (
	fulfillment,
	order,
	payment,
	return_order,
	shipment,
)
