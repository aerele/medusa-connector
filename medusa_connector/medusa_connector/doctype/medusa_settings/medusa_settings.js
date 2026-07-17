// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medusa Settings", {
	refresh(frm) {
		toggle_buttons(frm);
		setup_order_queries(frm);
		load_naming_series(frm);
		frm.set_query("price_list", () => ({
			filters: { selling: 1 },
		}));
		frm.set_query("erpnext_warehouse", "warehouse_mapping", () => ({
			filters: { disabled: 0 },
		}));
	},

	enabled(frm) {
		toggle_buttons(frm);
	},

	update_erpnext_stock_levels_to_medusa(frm) {
		toggle_buttons(frm);
	},

	fetch_medusa_locations(frm) {
		if (!frm.doc.enabled) {
			frappe.msgprint(__("Enable the Medusa Connector first."));
			return;
		}
		frm.call({
			doc: frm.doc,
			method: "fetch_medusa_locations",
			freeze: true,
			freeze_message: __("Fetching stock locations from Medusa…"),
			callback: () => {
				frm.refresh_field("warehouse_mapping");
				frm.dirty();
			},
		});
	},
});

function toggle_buttons(frm) {
	frm.remove_custom_button(__("Sync Webhooks"));
	frm.remove_custom_button(__("Regenerate Webhook Secret"));
	frm.remove_custom_button(__("View Webhook Logs"));
	frm.remove_custom_button(__("Sync Inventory Now"));

	if (!frm.doc.enabled) {
		return;
	}

	// —— Inventory ——
	if (frm.doc.update_erpnext_stock_levels_to_medusa && has_enabled_warehouse_mapping(frm)) {
		frm.add_custom_button(
			__("Sync Inventory Now"),
			() => {
				frappe.call({
					method: "medusa_connector.product.inventory_export.sync_inventory_now",
					freeze: true,
					freeze_message: __("Pushing ERPNext stock levels to Medusa…"),
					callback: (r) => {
						const m = r.message || {};
						frm.reload_doc();
						if (m.status === "Success") {
							frappe.show_alert({ message: m.message, indicator: "green" });
						} else if (m.status === "Busy" || m.status === "Skipped") {
							frappe.show_alert({ message: m.message, indicator: "blue" });
						} else if (m.status === "Partial Success") {
							frappe.show_alert({ message: m.message, indicator: "orange" });
						} else {
							frappe.msgprint({
								title: __("Inventory Sync"),
								message: m.message || __("Inventory sync failed."),
								indicator: "red",
							});
						}
					},
				});
			},
			__("Inventory")
		);
	}

	if (frm.doc.connection_status !== "Connected") {
		return;
	}

	// —— Webhooks ——
	if (frm.doc.webhook_secret) {
		frm.add_custom_button(
			__("Sync Webhooks"),
			() => {
				frappe.call({
					method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.sync_webhooks",
					freeze: true,
					freeze_message: __("Syncing webhooks with Medusa…"),
					callback: (r) => {
						const m = r.message || {};
						frm.reload_doc();
						if (m.status === "Installed") {
							frappe.show_alert({ message: m.message, indicator: "green" });
						} else if (m.status === "Busy") {
							frappe.show_alert({ message: m.message, indicator: "blue" });
						} else if (m.status === "Not Installed") {
							frappe.msgprint({
								title: __("Medusa Webhooks Plugin Not Installed"),
								message: m.instructions || m.message,
								indicator: "orange",
							});
						} else {
							frappe.msgprint({
								title: __("Webhook Sync Failed"),
								message:
									(m.instructions ? m.instructions + "<hr>" : "") +
									(m.message || __("Unknown error")),
								indicator: "red",
							});
						}
					},
				});
			},
			__("Webhooks")
		);
	}
	const hasWebhookSecret = Boolean(frm.doc.webhook_secret);

	frm.add_custom_button(
		__(hasWebhookSecret ? "Regenerate Webhook Secret" : "Generate Webhook Secret"),
		() => {
			const message = hasWebhookSecret
				? __(
						"This invalidates the current Medusa subscriber secret until you re-sync webhooks. Continue?"
				  )
				: __("Generate a webhook secret for secure webhook authentication?");

			frappe.confirm(message, () => {
				frappe.call({
					method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.regenerate_webhook_secret",
					callback: (r) => {
						if (r.message) {
							frappe.msgprint(r.message);
						}
						frm.reload_doc();
					},
				});
			});
		},
		__("Webhooks")
	);

	frm.add_custom_button(
		__("View Webhook Logs"),
		() => {
			// Webhooks and sync jobs share Ecommerce Integration Log.
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Medusa Connector",
				method: ["like", "%dispatch_event%"],
			});
		},
		__("Webhooks")
	);
}

function has_enabled_warehouse_mapping(frm) {
	const rows = frm.doc.warehouse_mapping || [];
	return rows.some((r) => r.enabled && r.erpnext_warehouse && r.medusa_location_id);
}

function setup_order_queries(frm) {
	frm.set_query("cost_center", () => ({
		filters: {
			company: frm.doc.company,
			is_group: 0,
		},
	}));
	frm.set_query("cash_bank_account", () => ({
		filters: {
			company: frm.doc.company,
			account_type: ["in", ["Cash", "Bank"]],
			is_group: 0,
		},
	}));
	const tax_query = () => ({
		filters: {
			company: frm.doc.company,
			account_type: ["in", ["Tax", "Chargeable", "Expense Account"]],
			is_group: 0,
		},
	});
	frm.set_query("default_sales_tax_account", tax_query);
	frm.set_query("default_shipping_charges_account", tax_query);
	frm.set_query("shipping_item", () => ({
		filters: { is_sales_item: 1, disabled: 0 },
	}));
}

function load_naming_series(frm) {
	if (!frm.doc.enabled) {
		return;
	}
	frappe.call({
		method: "ecommerce_core.utils.naming_series.get_series",
		callback(r) {
			if (!r.message) {
				return;
			}
			const set_opts = (field, key) => {
				const opts = r.message[key];
				if (opts) {
					frm.set_df_property(field, "options", opts);
				}
			};
			set_opts("sales_order_series", "sales_order_series");
			set_opts("sales_invoice_series", "sales_invoice_series");
			set_opts("delivery_note_series", "delivery_note_series");
		},
	});
}
