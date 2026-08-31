# Medusa Connector

Medusa Connector integrates Medusa v2 with ERPNext through Ecommerce Core. It converts Medusa commerce activity into traceable ERPNext selling, stock, and accounting documents while keeping ERPNext authoritative for inventory and accounting.

## What the connector synchronizes

### Products, variants, and inventory

ERPNext Items are synchronized with Medusa products and variants. Medusa stock locations are mapped to ERPNext warehouses through Ecommerce Core warehouse mappings. Inventory updates use those mappings so stock is attributed to the correct physical location rather than a connector-wide fallback.

### Customers and addresses

Orders resolve or create the corresponding ERPNext Customer and billing and shipping addresses. Medusa customer and address identifiers are stored on ERPNext records so repeated events update the same records instead of creating duplicates.

### Orders and lifecycle state

A Medusa order creates one ERPNext Sales Order containing the mapped customer, addresses, items, shipping charges, taxes, discounts, currency, warehouses, and Medusa identifiers. Payment, invoice, fulfillment, shipment, cancellation, return, claim, exchange, and refund states are subsequently projected onto the linked ERPNext documents.

The connector hydrates the complete Medusa order state, including items, variants, adjustments, tax lines, shipping methods, payment collections, refunds, fulfillments, returns, claims, exchanges, transactions, credits, and computed totals. Historical synchronization uses the same lifecycle pipeline as webhooks and can recover missed events safely.

### Discounts and promotions

Medusa computed item and shipping adjustments are authoritative. The connector supports the resulting values from fixed-amount, percentage, order-wide, free-shipping, and Buy-X-Get-Y promotions. Item discounts are represented on Sales Order rows, while discounted shipping is represented by the computed shipping charge.

Medusa line identities are stored on ERPNext child rows for reliable reconciliation. Before submission, the ERPNext grand total is compared with the Medusa order total; a mismatch stops synchronization instead of creating a financially incorrect document. Existing orders are checked by the same invariant so historical discount errors cannot pass silently.

Gift cards and credit lines are treated as financial credits, not product discounts, preserving the distinction between promotion value and tender or account credit.

### Taxes and pricing

Both tax-exclusive and tax-inclusive Medusa pricing are supported. The connector uses Medusa net subtotals and computed item and shipping tax lines, preventing inclusive tax from being added twice. Taxes are mapped to configured ERPNext accounts and may be consolidated while retaining item-wise tax detail.

Medusa computed totals remain authoritative, with ERPNext currency precision applied only at document rounding boundaries.

### Payments, invoices, and fulfillments

Captured Medusa payments can create linked ERPNext Payment Entries and Sales Invoices according to Medusa Settings. Fulfillments create Delivery Notes for the fulfilled quantities and warehouse mappings. Shipment and cancellation events update or reverse the appropriate linked documents without duplicating previously processed events.

Every document carries the relevant Medusa order, payment, fulfillment, and transaction identifiers for auditability and idempotency.

### Returns

A requested return updates the synchronized order state but does not move inventory. Once Medusa reports the return as received, the connector creates an ERPNext Return Delivery Note against the submitted outbound Delivery Note.

Sellable quantities are received into Return Warehouse. Lines reported as damaged are routed to Damaged Return Warehouse. A missing required warehouse or missing source Delivery Note stops processing with an actionable error rather than posting inventory incorrectly.

### Claims

Refund claims use the refund accounting workflow. Replacement claims process the inbound returned goods and create a separate outbound Delivery Note for replacement items. Claim identifiers are stored on all resulting documents, making webhook retries idempotent.

### Exchanges

An exchange is represented as two explicit stock movements: a received return against the original outbound Delivery Note and a standalone outbound Delivery Note for the replacement items. This preserves the audit trail for both directions and allows each movement to be retried safely.

### Refunds

Full and partial refunds create ERPNext Credit Notes against the original submitted Sales Invoice. When a Cash / Bank Account is configured, the connector also creates the corresponding refund Payment Entry.

Partial refunds do not cancel the original invoice or receipt. Medusa refund identifiers prevent the same refund from being applied twice.

## Reliability and audit behavior

Webhook processing is idempotent by Medusa order, order-line, payment, refund, fulfillment, return, claim, exchange, and transaction identifiers. The webhook endpoint validates the configured signature and queues processing. Provider credentials and personal or payment payloads are redacted from integration logs.

Full order reconciliation is the recovery mechanism for missed or out-of-order events. Submitted historical ERPNext documents with total mismatches are reported for controlled reconciliation; they are not silently cancelled or amended.

## Configuration

1. Install `erpnext`, `ecommerce_core`, and `medusa_connector`, then run `bench --site <site> migrate`.
2. Open Medusa Settings and configure the Medusa Base URL and Admin API Key.
3. Select Company, Selling Price List, Customer Group, Item Group, Stock UOM, and default Warehouse.
4. Configure Return Warehouse and Damaged Return Warehouse before receiving returned goods.
5. Map every Medusa stock location to its ERPNext Warehouse.
6. Map Medusa tax rates and shipping options to ERPNext accounts, and configure the default tax and shipping accounts.
7. Configure Sales Order, Sales Invoice, Delivery Note, and Payment Entry behavior and naming series as required.
8. Set a Cash / Bank Account when captured-payment or refund Payment Entries should be created.
9. Enable the connector and use Sync Webhooks to register connector-owned order, payment, fulfillment, return, claim, and exchange events.
10. Use Sync Orders with an explicit date range for historical import or current-state reconciliation. Repeated synchronization is safe.

## Operational expectations

ERPNext stock rules remain enforced. Delivery, replacement, or return documents will not be forced through insufficient stock, missing warehouse mappings, or invalid accounting configuration. Correct those operational conditions and retry the Medusa event or order reconciliation.

Use Ecommerce Integration Log to monitor queued, successful, skipped, and failed operations. Each log entry records the operation and relevant Medusa identity without exposing configured secrets.
