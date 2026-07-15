# Order sync (Medusa → ERPNext)

Follows **Ecommerce Core**: webhook or service call creates a **Sales Order**,
with customer/address/contact created at order time, and optional Sales Invoice on payment.

## Flow

```text
order.placed / sync_sales_order / Sync Orders bulk job
 → ensure_order_customer (customer.sync → EcommerceCustomer)
 → ensure_order_items (import product if Ecommerce Item missing)
 → create Sales Order if missing (idempotent on medusa_order_id)
 → apply_order_lifecycle (current Medusa state):
      · refresh status fields on SO / SI / DN
      · payment_status captured/paid → SI + PE (if enabled)
      · each fulfillment → DN (or cancel DN if fulfillment canceled)
      · order status canceled → cancel SO if safe, else status only
      · payment_status refunded → status stamp (credit note pending)
```

Idempotency: one Sales Order per `medusa_order_id`; one SI per order; one Delivery Note per `medusa_fulfillment_id`.

## Shared core

| Concern | Module |
|---------|--------|
| Customer / Address / Contact | `ecommerce_core.controllers.customer.EcommerceCustomer` via `customer.sync` |
| Dummy price list / tax category | `ecommerce_core.utils.price_list`, `ecommerce_core.utils.taxation` |
| Item mapping | `Ecommerce Item` (`integration_item_code` = Variant ID) |
| Logging | `Ecommerce Integration Log` via `create_medusa_log` |

## Settings (Orders tab)

- Company, Cost Center, Sales Order Series
- Default Warehouse (Products tab — used on SO lines)
- Default Customer (guests)
- Default Sales Tax / Shipping Accounts
- Sync Sales Invoice on Payment, series, Cash/Bank Account
- Sync Delivery Note on Fulfillment, Delivery Note Series
- Consolidate Taxes, Shipping as Item

### Sync Orders (full lifecycle bulk)

On **Medusa Settings → Orders → Sync Orders**:

1. Check **Sync Orders**
2. Set **From** and **To** (Datetime)
3. Save

Behaviour:

- Job runs after save (long queue) and also on the `hourly_long` scheduler.
- Loads Medusa orders with `created_at` between From and To (inclusive).
- For **every** order in range (including those that already have a Sales Order):
  - Ensures SO exists
  - Syncs payment → SI/PE when Medusa shows captured/paid
  - Syncs fulfillments → Delivery Notes (partial / multi-ship)
  - Applies cancel / refund **status** from Medusa
- Does **not** skip existing SOs — lifecycle catch-up is intentional.
- Clears the **Sync Orders** checkbox when finished.
- Logs progress in **Ecommerce Integration Log**.

```python
from medusa_connector.order.sync import sync_orders, sync_sales_order, apply_order_lifecycle

sync_orders()  # bulk (reads Medusa Settings From/To)
sync_sales_order(full_order_dict)  # single order, full lifecycle
```

## Custom fields

| DocType | Fields |
|---------|--------|
| Sales Order / Invoice / Delivery Note | `medusa_order_id`, `medusa_order_number`, `medusa_order_status` |
| Delivery Note | `medusa_fulfillment_id` (one DN per fulfillment) |
| Sales Order Item | `medusa_item_discount` |

## Entry points

```python
from medusa_connector.order.sync import (
	sync_sales_order,
	apply_order_lifecycle,
	cancel_order,
	sync_orders,  # alias: sync_old_orders
)

sync_sales_order(order_dict)  # webhook / single — full lifecycle
sync_orders()  # bulk From/To — full lifecycle
```

## Webhooks

| Event | Behaviour |
|-------|-----------|
| `order.placed` | Create SO |
| `order.updated` / `completed` | Create if missing; else refresh status |
| `order.canceled` | Cancel SO if no SI/DN; else status only |
| `payment.captured` | Resolve order via Admin payment → `payment_collection.order`, ensure SO + optional SI + PE |
| `payment.refunded` | Order resolved the same way; refund document sync pending |
| `order.fulfillment_created` | Ensure SO; create DN for `fulfillment_id` (partial / multi-ship OK) |
| `order.fulfillment_canceled` | Cancel DN for that fulfillment when safe |
| `shipment.created` / `delivery.created` | Ensure DN + refresh tracking / status |
| Return | Pending |

### Delivery Note sync (Medusa v2)

Mirrors Shopify: **one Delivery Note per fulfillment**, built with `make_delivery_note(so)` then filtered to fulfillment line items/qty. Warehouse from location mapping or default warehouse.

```text
order.fulfillment_created {order_id, fulfillment_id}
  → GET /admin/orders/{order_id}?fields=…,*fulfillments,*fulfillments.items
  → find fulfillment by id
  → ensure Sales Order (create if missing)
  → if sync_delivery_note:
       if DN exists (medusa_fulfillment_id): update status/tracking
       else: make_delivery_note → filter items → submit
```

Idempotency key: `Delivery Note.medusa_fulfillment_id`.

### Payment capture resolution (Medusa v2)

Webhook body is thin: `{"id": "pay_..."}` (no `order_id`).

```text
payment.captured {id: pay_…}
  → GET /admin/payments/{id}?fields=…,*payment_collection,*payment_collection.order
  → order_id = payment.payment_collection.order.id
  → GET /admin/orders/{order_id} (full fields)
  → sync_sales_order (idempotent)
  → create_sales_invoice (+ Payment Entry; PE.reference_no = pay_… for idempotency)
```
