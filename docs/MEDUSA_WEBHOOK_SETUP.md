# Medusa → ERPNext Webhook Setup

This guide explains how to configure Medusa to forward supported events to ERPNext using the `@lambdacurry/medusa-webhooks` plugin and an HMAC-SHA256 signed subscriber.

---

## Step 1 — Install the webhook plugin

Install the plugin in the Medusa project:

```bash
npm install @lambdacurry/medusa-webhooks
```

---

## Step 2 — Define ERPNext webhook events

Create:

```text
src/lib/erpnext-webhook-events.ts
```

Add:

```ts
// src/lib/erpnext-webhook-events.ts

export const ERPNEXT_WEBHOOK_EVENTS = [
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
  "order.completed",
  "order.canceled",

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

  // Inventory
  "inventory.inventory-item.created",
  "inventory.inventory-item.updated",
  "inventory.inventory-item.deleted",
  "inventory.inventory-level.updated",
] as const

export type ErpnextWebhookEvent = (typeof ERPNEXT_WEBHOOK_EVENTS)[number]
```

This file is used by both `medusa-config.ts` and the `forward-to-erpnext.ts` subscriber.

---

## Step 3 — Register the plugin

Import the event list into `medusa-config.ts`:

```ts
import { ERPNEXT_WEBHOOK_EVENTS } from "./src/lib/erpnext-webhook-events"
```

Register the plugin:

```ts
plugins: [
  {
    resolve: "@lambdacurry/medusa-webhooks",
    options: {
      subscriptions: [...ERPNEXT_WEBHOOK_EVENTS],
    },
  },
],
```

Example:

```ts
module.exports = defineConfig({
  // ...

  plugins: [
    {
      resolve: "@lambdacurry/medusa-webhooks",
      options: { subscriptions: [...ERPNEXT_WEBHOOK_EVENTS] },
    },
  ],
})
```

---

## Step 4 — Configure the HMAC signing secret

In **ERPNext → Medusa Settings → Inbound Webhooks**:

1. Click **Generate Secret Key**.
2. The generated secret is stored in the **Webhook Secret** field.
3. Configure the same secret in the Medusa environment:

```env
ERPNEXT_WEBHOOK_SIGNING_SECRET=your-secure-webhook-secret
```

The secret must be the same on both sides.

It is used only to generate and verify the HMAC-SHA256 signature.

---

## Step 5 — Add the ERPNext forwarder subscriber

Create:

```text
src/subscribers/forward-to-erpnext.ts
```

Add:

```ts
// src/subscribers/forward-to-erpnext.ts

import crypto from "crypto"
import {
  SubscriberArgs,
  SubscriberConfig,
} from "@medusajs/framework/subscribers"
import {
  getWebhooksSubscriptionsWorkflow,
} from "@lambdacurry/medusa-webhooks/workflows"
import { ERPNEXT_WEBHOOK_EVENTS } from "../lib/erpnext-webhook-events"

function toConnectorEventName(name: string): string {
  if (name.startsWith("inventory.inventory-item.")) {
    return name.replace("inventory.inventory-item.", "inventory-item.")
  }
  if (name.startsWith("inventory.inventory-level.")) {
    return name.replace("inventory.inventory-level.", "inventory-level.")
  }
  return name
}

function signPayload(timestamp: string, bodyString: string, secret: string): string {
  return crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}.${bodyString}`, "utf8")
    .digest("hex")
}

export const config: SubscriberConfig = {
  event: [...ERPNEXT_WEBHOOK_EVENTS],
  context: {
    subscriberId: "erpnext-webhook-forwarder",
  },
}

export default async function forwardToErpnext({
  event,
  container,
}: SubscriberArgs<{ id: string }>) {
  const logger = container.resolve("logger")
  const eventName = toConnectorEventName(event.name)

  const webhookEventId = event.metadata?.eventGroupId
    ? `${event.name}:${event.metadata.eventGroupId}`
    : `${event.name}:${event.data.id}`

  const payload = {
    ...event.data,
    webhook_event_id: webhookEventId,
    event_metadata: event.metadata,
  }

  const { result } = await getWebhooksSubscriptionsWorkflow(container).run({
    input: { eventName, eventData: payload },
  })

  const subscriptions = (result as unknown as { results: { target_url: string; active: boolean }[] })
    ?.results ?? []

  const secret = process.env.ERPNEXT_WEBHOOK_SIGNING_SECRET
  if (!secret) {
    logger.error("ERPNEXT_WEBHOOK_SIGNING_SECRET is not configured — skipping send")
    return
  }

  const bodyString = JSON.stringify({ event: eventName, data: payload })
  const timestamp = Math.floor(Date.now() / 1000).toString()
  const signature = signPayload(timestamp, bodyString, secret)

  for (const sub of subscriptions) {
    if (sub.active === false) continue

    try {
      const res = await fetch(sub.target_url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Medusa-Signature": `sha256=${signature}`,
          "X-Medusa-Timestamp": timestamp,
          "X-Medusa-Event": eventName,
          "X-Medusa-Event-Id": webhookEventId,
        },
        body: bodyString,
      })

      if (res.ok) {
        logger.info(`ERPNext webhook delivered: ${eventName} -> ${sub.target_url}`)
      } else {
        logger.error(`ERPNext webhook failed (${res.status}): ${eventName} -> ${sub.target_url}`)
      }
    } catch (err) {
      logger.error(`ERPNext webhook errored: ${eventName} -> ${sub.target_url}: ${err}`)
    }
  }
}
```

---

## Step 6 — Migrate and restart Medusa

Run:

```bash
npx medusa db:migrate
```

Then restart Medusa:

```bash
npm run dev
```

For production, restart using the normal Medusa production command.

---

## Step 7 — Register webhooks from ERPNext

In **ERPNext → Medusa Settings**:

1. Make sure the Medusa connection is configured and verified.
2. Make sure the Webhook Secret has been generated under **Inbound Webhooks**.
3. Click **Sync Webhooks**.

ERPNext will register the supported webhook events in Medusa using the following endpoint:

```text
<your-erpnext-site>/api/method/medusa_connector.api.webhook.receive
```


The Webhook Secret is used only for HMAC-SHA256 signature generation and verification.


---

## Setup Complete

The webhook integration is ready when:

* `@lambdacurry/medusa-webhooks` is installed in Medusa.
* `ERPNEXT_WEBHOOK_EVENTS` is configured.
* The plugin is registered in `medusa-config.ts`.
* The forwarder subscriber is added.
* `ERPNEXT_WEBHOOK_SIGNING_SECRET` is configured in Medusa.
* The same secret is configured in **ERPNext → Medusa Settings → Inbound Webhooks**.
* Medusa has been migrated and restarted.
* **Sync Webhooks** has been executed from ERPNext.
* The webhook registrations are shown as **Registered** in ERPNext.

No manual webhook URL configuration is required in Medusa after the initial setup.
