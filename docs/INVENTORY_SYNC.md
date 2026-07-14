# Inventory sync (ERPNext → Medusa)

Shopify-style **one-way** stock push. ERPNext is the quantity source of truth.

## Settings layout

| Tab | Purpose |
|-----|---------|
| **(main)** | Connection + store defaults |
| **Customers** | Customer defaults |
| **Products** | Catalog import/export, item defaults, product sync status |
| **Inventory** | Stock push enable, frequency, **warehouse ↔ location mapping** |
| **Inbound Webhooks** | Webhook secret and registrations |

Products and inventory are separate on purpose: product import never loads store qty.

## Multi warehouse ↔ location

On the **Inventory** tab:

1. Enable **Update Stock Levels to Medusa**.
2. Click **Fetch Medusa Locations** (loads `GET /admin/stock-locations`).
3. Set **ERPNext Warehouse** on each row you want to sync and leave **Enabled** checked.
4. Save.

Rules:

- One ERPNext warehouse → one Medusa location (1:1).
- Group warehouses are rolled up to leaf bins (same idea as Unicommerce).
- Existing warehouse links are preserved when you re-fetch locations.

If the mapping table is empty but **Default Warehouse** + **Default Location ID** exist, the connector seeds one mapping row automatically on save (migration helper).

## Behaviour

1. Scheduler (`hooks.py` → `all`) calls `update_inventory_on_medusa`.
2. Frequency comes from **Inventory Sync Frequency**.
3. For each enabled mapping: dirty bins for that warehouse are selected.
4. Available qty = `max(actual_qty - reserved_qty, 0)` (integer).
5. Qty is written to the mapped Medusa location.
6. Item mapping `inventory_synced_on` is stamped; results go to **Ecommerce Integration Log**.

## Onboarding

1. Connect Medusa and import products (catalog only).
2. Open stock in ERPNext (Stock Reconciliation / Material Receipt).
3. Configure warehouse mapping on the Inventory tab.
4. Enable inventory sync and save.
5. Use **Sync Inventory Now** or wait for the scheduler.

Enabling with **zero** ERP stock can push **0** and overwrite Medusa quantities.

## What is not synced

- Medusa Admin qty → ERP Bin
- Product import opening stock
- Inventory-item webhooks only update Item attributes (not Bin qty)
