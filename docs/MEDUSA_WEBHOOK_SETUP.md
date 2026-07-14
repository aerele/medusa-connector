# Medusa → ERPNext Webhook Setup

This guide explains the **one-time setup on the Medusa side** so the Medusa
Connector can receive order/payment/fulfillment/shipment/return events in ERPNext.

ERPNext is the source of truth and drives the webhook **registration/management**
from the **Medusa Settings** page. On the Medusa side you install the webhooks
plugin and add **one small generic forwarder subscriber** (the plugin requires it —
see Step 5). You do not write any business logic in Medusa; ERPNext owns that.

---

## Why a plugin + subscriber are needed

Medusa v2 has **no built-in outbound webhooks**. The supported community plugin
**[`@lambdacurry/medusa-webhooks`](https://medusajs.com/integrations/lambdacurry-webhooks/)**
adds a webhook subscriptions model + admin API that the connector talks to.

Per the plugin's own docs, it **does not auto-wire subscribers** — you must add one
generic subscriber file that forwards events into the plugin's workflow. It is glue,
not business logic, and a single file covers every event.

---

## Prerequisites

- A running Medusa v2 server you can deploy code to.
- The Medusa **Admin API Key** (`sk_...`) and **Base URL** already entered and
  **Connected** in *Medusa Settings* (the connector uses the Admin REST API).
- Connection Mode = **REST**.

---

## Step 1 — Install the plugin in Medusa

In your Medusa project:

```bash
npm install @lambdacurry/medusa-webhooks
```

> **Monorepo/workspace note:** if your project is a workspace (e.g. `apps/backend`)
> use the repo's package manager. pnpm: `pnpm add @lambdacurry/medusa-webhooks --filter backend`.
> yarn: `yarn workspace backend add @lambdacurry/medusa-webhooks`. npm: add
> `-w apps/backend --legacy-peer-deps` (the plugin pins `@medusajs/* @2.17.0` peers).

## Step 2 — Register the plugin

Add it to `medusa-config.ts` under `plugins`, listing the events in `subscriptions`:

```ts
module.exports = defineConfig({
  // ...
  plugins: [
    {
      resolve: "@lambdacurry/medusa-webhooks",
      options: {
        subscriptions: [
          // Product
          "product.created",
          "product.updated",
          "product.deleted",

          // Product Variant
          "product-variant.created",
          "product-variant.updated",
          "product-variant.deleted",

          // Product Category
          "product-category.created",
          "product-category.updated",
          "product-category.deleted",

          // Product Collection
          "product-collection.created",
          "product-collection.updated",
          "product-collection.deleted",

          // Customer
          "customer.created",
          "customer.updated",
          "customer.deleted",

          // Order
          "order.placed",
          "order.updated",
          "order.canceled",
          "order.completed",

          // Payment
          "payment.captured",
          "payment.refunded",

          // Fulfillment
          "order.fulfillment_created",
          "order.shipment_created",
          "fulfillment.canceled",

          // Returns
          "order.return_requested",
          "order.return_received",

          // Inventory (ERPNext short names; see Step 5 note on module event mapping)
          "inventory-item.created",
          "inventory-item.updated",
          "inventory-item.deleted",
          "inventory-level.updated",

          // Price
          "price-list.created",
          "price-list.updated",
          "price-list.deleted",
          "price-set.created",
          "price-set.updated",
          "price-set.deleted",

          // Region
          "region.created",
          "region.updated",
          "region.deleted",

          // Sales Channel
          "sales-channel.created",
          "sales-channel.updated",
          "sales-channel.deleted",

          // Stock Location
          "stock-location.created",
          "stock-location.updated",
          "stock-location.deleted",
        ],
      },
    },
  ],
})
```

## Step 3 — Add the forwarder subscriber (required)

The plugin does not auto-wire subscribers, so add ONE generic file at
`src/subscribers/forward-to-erpnext.ts`:

```ts
import { SubscriberArgs, SubscriberConfig } from "@medusajs/framework/subscribers"
import { fullWebhooksSubscriptionsWorkflow } from "@lambdacurry/medusa-webhooks/workflows"

export const config: SubscriberConfig = {
  event: [
    // Product
    "product.created",
    "product.updated",
    "product.deleted",

    // Product Variant
    "product-variant.created",
    "product-variant.updated",
    "product-variant.deleted",

    // Product Category
    "product-category.created",
    "product-category.updated",
    "product-category.deleted",

    // Product Collection
    "product-collection.created",
    "product-collection.updated",
    "product-collection.deleted",

    // Customer
    "customer.created",
    "customer.updated",
    "customer.deleted",

    // Order
    "order.placed",
    "order.updated",
    "order.canceled",
    "order.completed",

    // Payment
    "payment.captured",
    "payment.refunded",

    // Fulfillment
    "order.fulfillment_created",
    "order.shipment_created",
    "fulfillment.canceled",

    // Returns
    "order.return_requested",
    "order.return_received",

    // Inventory — Medusa *module* event names (what is actually emitted).
    // Core inventory workflows do not emit short names like inventory-item.updated.
    // Map these to connector short names in the handler (see toConnectorEventName below).
    "inventory.inventory-item.created",
    "inventory.inventory-item.updated",
    "inventory.inventory-item.deleted",
    "inventory.inventory-level.updated",

    // Price
    "price-list.created",
    "price-list.updated",
    "price-list.deleted",
    "price-set.created",
    "price-set.updated",
    "price-set.deleted",

    // Region
    "region.created",
    "region.updated",
    "region.deleted",

    // Sales Channel
    "sales-channel.created",
    "sales-channel.updated",
    "sales-channel.deleted",

    // Stock Location
    "stock-location.created",
    "stock-location.updated",
    "stock-location.deleted",
  ],
  context: { subscriberId: "erpnext-webhook-forwarder" },
}

/** Map Inventory module event names → ERPNext connector short names. */
function toConnectorEventName(name: string): string {
  if (name.startsWith("inventory.inventory-item.")) {
    return name.replace("inventory.inventory-item.", "inventory-item.")
  }
  if (name.startsWith("inventory.inventory-level.")) {
    return name.replace("inventory.inventory-level.", "inventory-level.")
  }
  return name
}

export default async function forwardToErpnext({
  event, container,
}: SubscriberArgs<{ id: string }>): Promise<void> {
  await fullWebhooksSubscriptionsWorkflow(container).run({
    input: {
      eventName: toConnectorEventName(event.name),
      eventData: event.data,
    },
  })
}
```

> **Why inventory uses different names:** Medusa’s Inventory *module* emits
> `inventory.inventory-item.updated` (and related) via internal `@EmitEvents`.
> Core inventory workflows do **not** call `emitEventStep`, so short names like
> `inventory-item.updated` are never fired. The forwarder must subscribe to the
> module names and map them to the connector’s short names so **Sync Webhooks**
> subscriptions match.

The `subscriptions` option, this subscriber's `event` list, and the ERPNext connector's registered event handlers should remain in sync. When support for a new Medusa resource is added, update all three together and re-run **Sync Webhooks**.

## Step 4 — Migrate and restart Medusa

```bash
npx medusa db:migrate
npm run dev      # or your production start command
```

---

## Step 5 — Let ERPNext register the webhooks

Back in **ERPNext → Medusa Settings**:

1. Make sure the connector is **Enabled** and the connection is verified.
2. Set/generate a **Webhook Secret** (used to authenticate incoming calls).
3. Click **Sync Webhooks**.

The connector will:

- Detect the plugin (**Webhook Plugin Status → Installed**).
- Register every required event to your ERP endpoint.
- Remove any duplicate/stale registrations.
- Fill the **Webhooks** table with each event's *Webhook ID*, *ERP Endpoint*,
  *Registration Status*, *Last Sync Time*, and *Last Error*.

The ERP endpoint it registers is:

```
<your-erpnext-site>/api/method/medusa_connector.api.webhook.receive?token=<webhook secret>
```

You never paste this into Medusa by hand — the connector registers it via the
plugin's admin API.

---

## Events registered
| Category           | Medusa events                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| Product            | `product.created`, `product.updated`, `product.deleted`                                                                         |
| Product Variant    | `product-variant.created`, `product-variant.updated`, `product-variant.deleted`                                                 |
| Product Category   | `product-category.created`, `product-category.updated`, `product-category.deleted`                                              |
| Product Collection | `product-collection.created`, `product-collection.updated`, `product-collection.deleted`                                        |
| Customer           | `customer.created`, `customer.updated`, `customer.deleted`                                                                      |
| Order              | `order.placed`, `order.updated`, `order.canceled`, `order.completed`                                                            |
| Payment            | `payment.captured`, `payment.refunded`                                                                                          |
| Fulfillment        | `order.fulfillment_created`, `order.shipment_created`, `fulfillment.canceled`                                                   |
| Return             | `order.return_requested`, `order.return_received`                                                                               |
| Inventory          | `inventory-item.created`, `inventory-item.updated`, `inventory-item.deleted`, `inventory-level.updated`                         |
| Price              | `price-list.created`, `price-list.updated`, `price-list.deleted`, `price-set.created`, `price-set.updated`, `price-set.deleted` |
| Region             | `region.created`, `region.updated`, `region.deleted`                                                                            |
| Sales Channel      | `sales-channel.created`, `sales-channel.updated`, `sales-channel.deleted`                                                       |
| Stock Location     | `stock-location.created`, `stock-location.updated`, `stock-location.deleted`                                                    |

Adding a new event later is a one-file change in the connector (a handler class);
re-run **Sync Webhooks** and it registers automatically.

---

## Authentication note

The plugin's subscription model stores only `{event_type, target_url, active}` —
no secret/header field — so it cannot HMAC-sign requests. The connector therefore
authenticates by embedding the **Webhook Secret as a `token` query parameter** in
the registered URL and validating it on receipt (constant-time compare). Keep
**Verify Signatures** on. If you later use a signing-capable sender, the receiver
also accepts a real HMAC signature header.

---

## Verifying it works

1. In Medusa, trigger an event (e.g. place a test order).
2. In ERPNext, open **Ecommerce Integration Log** (filter Integration = Medusa Connector) —
   you should see a row move `Queued → Success` (message like `webhook:{event_id}`).
3. Failed events are retried automatically (every 10 minutes, up to 5 attempts).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| **Webhook Plugin Status = Not Installed** | Plugin not installed/migrated on Medusa. Repeat Steps 1–4, restart Medusa, click Sync Webhooks. |
| **Webhook Plugin Status = Error** | Base URL/API key wrong or Medusa unreachable. Fix connection, re-sync. |
| **Log status = Error (401 / rejected)** | Secret mismatch. Re-generate the Webhook Secret and click Sync Webhooks to re-register with the new token. |
| **Log status = Error** | Handler error; see the log's *Traceback* / *Response Data*. Auto-retried by the scheduler. |
