# Ecommerce Core integration

Medusa Connector depends on **ecommerce_core** for shared masters and logging.

## Shared doctypes

| Legacy (deleted) | Ecommerce Core |
|------------------|----------------|
| Medusa Item Mapping *(deleted)* | **Ecommerce Item** (`integration = Medusa Connector`) |
| Medusa Sync Log *(deleted)* | **Ecommerce Integration Log** |
| Medusa Webhook Log *(deleted)* | **Ecommerce Integration Log** (message `webhook:{event_id}`) |

## Field mapping (items)

Medusa always has a sellable **Variant** (including single-variant / “simple” products).

| Ecommerce Item | Sellable (variant / simple) | Template (ERPNext has_variants) |
|----------------|----------------------------|----------------------------------|
| `integration` | `Medusa Connector` | `Medusa Connector` |
| `integration_item_code` | **Medusa Variant ID** | Medusa Product ID (product anchor) |
| `variant_id` | Medusa Variant ID | empty |
| `sku` | Medusa variant SKU / ERPNext item code | empty |
| `variant_of` | ERPNext template Item (if any) | empty |
| `has_variants` | `0` | `1` |
| `erpnext_item_code` | Item | template Item |
| `inventory_synced_on` | last inventory push | — |

## Helpers — import directly from ecommerce_core

```python
from ecommerce_core.controllers.inventory import (
	get_inventory_levels,
	get_inventory_levels_of_group_warehouse,
	update_inventory_sync_status,
)
from ecommerce_core.controllers.scheduling import need_to_run
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log.ecommerce_integration_log import (
	create_log,
)
```

Medusa-specific logging wrappers (optional convenience) live in `medusa_connector.utils.logging`
and call `create_log` with `module_def="Medusa Connector"`.

Webhooks: `receive()` creates a log with `method=...dispatch_event` and enqueues processing. Retries use Ecommerce Integration Log resync patterns + Medusa scheduler sweep.
