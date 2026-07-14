# Customer sync (order-time only)

Follows **Shopify / Ecommerce Core**: customers are created when **orders** are
synced, not via a standalone customer import or live `customer.created` create.

## When ERPNext Customer is created

```text
order.placed / order sync
  → medusa_connector.customer.sync.ensure_order_customer(order)
       # (or resolve_order_customer — same module; do not re-export via order.sync)
       if medusa_customer_id not on any Customer:
            EcommerceCustomer.sync_customer  → Customer
            create_customer_address          → Address (Billing / Shipping)
            create_customer_contact          → Contact
       else:
            update_existing_addresses
  → continue Sales Order creation
```

Import customer helpers from their real module:

```python
from medusa_connector.customer.sync import ensure_order_customer, resolve_order_customer
```

## Identity

| ERPNext | Medusa |
|---------|--------|
| `Customer.medusa_customer_id` | `customer.id` |
| `Address.medusa_address_id` | `address.id` |

Email is **not** used for matching.

## Shared core

`MedusaCustomer` subclasses `ecommerce_core.controllers.customer.EcommerceCustomer`
and uses the same create methods as Shopify for Customer / Address / Contact.

Country codes use `ecommerce_core.utils.address_mapping.get_country_name`.

## Webhooks

| Event | Behaviour |
|-------|-----------|
| `customer.created` / `updated` | Acknowledged only (no ERP create) |
| `customer.deleted` | Soft-disable ERPNext Customer if present |

## Settings

| Field | Role |
|-------|------|
| Customer Group | Used on create |
| Default Customer | Guest / missing `customer_id` on order |

## Not implemented

- Bulk “Import Customers” job/button
- Instant customer create on customer webhooks
- Outbound Customer → Medusa
