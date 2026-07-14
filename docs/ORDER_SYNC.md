  # Order sync (Medusa → ERPNext)

  Follows **Shopify / Ecommerce Core**: webhook or service call creates a **Sales Order**,
  with customer/address/contact created at order time, and optional Sales Invoice on payment.

  ## Flow

  ```text
  order.placed (or sync_sales_order / sync_old_orders)
    → ensure_order_customer          (customer.sync → EcommerceCustomer)
    → ensure_order_items             (import product if Ecommerce Item missing)
    → create_sales_order             (items, taxes, shipping)
    → if payment captured + setting: create_sales_invoice (+ PE)
  ```

  Idempotency: one Sales Order per `medusa_order_id`.

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
  - Consolidate Taxes, Shipping as Item  

  ## Custom fields

  | DocType | Fields |
  |---------|--------|
  | Sales Order / Invoice / Delivery Note | `medusa_order_id`, `medusa_order_number`, `medusa_order_status` |
  | Sales Order Item | `medusa_item_discount` |

  ## Entry points

  ```python
  from medusa_connector.order.sync import sync_sales_order, cancel_order, sync_old_orders

  sync_sales_order(order_dict)          # webhook / single
  sync_old_orders(from_date=..., to_date=...)  # bulk backfill
  ```

  ## Webhooks

  | Event | Behaviour |
  |-------|-----------|
  | `order.placed` | Create SO |
  | `order.updated` / `completed` | Create if missing; else refresh status |
  | `order.canceled` | Cancel SO if no SI/DN; else status only |
  | `payment.captured` | Ensure SO + optional SI |
  | Fulfillment / return | Status note (DN sync later) |
