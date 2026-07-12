# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Central constants for the Medusa Connector (standalone app)."""

MODULE_NAME = "Medusa Connector"
SETTING_DOCTYPE = "Medusa Settings"
ITEM_MAPPING_DOCTYPE = "Medusa Item Mapping"
SYNC_LOG_DOCTYPE = "Medusa Sync Log"
WEBHOOK_LOG_DOCTYPE = "Medusa Webhook Log"

# Realtime / RQ job names for the Sync Products page.
PRODUCT_SYNC_JOB_NAME = "medusa.job.sync.products"
PRODUCT_SYNC_REALTIME_KEY = "medusa.key.sync.products"

# Default ERPNext values used when Settings leave them blank.
DEFAULT_ITEM_GROUP = "All Item Groups"
DEFAULT_STOCK_UOM = "Nos"
