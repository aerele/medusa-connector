// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medusa Settings", {
	refresh(frm) {
		toggle_buttons(frm);
		frm.set_query("price_list", () => ({
			filters: { selling: 1 },
		}));
	},

	enabled(frm) {
		if (!frm.doc.enabled) {
			toggle_buttons(frm);
		}
	},
});

function toggle_buttons(frm) {
	frm.remove_custom_button(__("Test Connection"));
	frm.remove_custom_button(__("Refresh Store Defaults"));
	frm.remove_custom_button(__("Sync Webhooks"));
	frm.remove_custom_button(__("Regenerate Webhook Secret"));
	frm.remove_custom_button(__("Sync Products"));
	frm.remove_custom_button(__("View Item Mappings"));
	frm.remove_custom_button(__("View Sync Logs"));
	frm.remove_custom_button(__("View Webhook Logs"));

	if (!frm.doc.enabled) {
		return;
	}

	frm.add_custom_button(
		__("Test Connection"),
		() => {
			frappe.call({
				method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.test_connection",
				freeze: true,
				freeze_message: __("Testing Medusa connection…"),
				callback: (r) => {
					const m = r.message || {};
					frm.reload_doc();
					const indicator =
						m.status === "Connected"
							? "green"
							: m.status === "Auth Failed"
							? "red"
							: "orange";
					frappe.show_alert({ message: m.message || m.status, indicator });
				},
			});
		},
		__("Connection")
	);

	frm.add_custom_button(
		__("Refresh Store Defaults"),
		() => {
			frappe.call({
				method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.refresh_store_defaults",
				freeze: true,
				freeze_message: __("Fetching Medusa store defaults…"),
				callback: (r) => {
					frm.reload_doc();
					const m = r.message || {};
					if (m.ok) {
						frappe.show_alert({ message: m.message, indicator: "green" });
					}
				},
			});
		},
		__("Connection")
	);

	frm.add_custom_button(
		__("Sync Products"),
		() => {
			frappe.set_route("medusa-sync-products");
		},
		__("Products")
	);

	frm.add_custom_button(
		__("View Item Mappings"),
		() => {
			frappe.set_route("List", "Medusa Item Mapping");
		},
		__("Products")
	);

	frm.add_custom_button(
		__("View Sync Logs"),
		() => {
			frappe.set_route("List", "Medusa Sync Log");
		},
		__("Products")
	);

	if (frm.doc.connection_status !== "Connected") {
		return;
	}

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

	frm.add_custom_button(
		__("Regenerate Webhook Secret"),
		() => {
			frappe.confirm(
				__(
					"This invalidates the current Medusa subscriber secret until you re-sync webhooks. Continue?"
				),
				() => {
					frappe.call({
						method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.regenerate_webhook_secret",
						callback: (r) => {
							if (r.message) {
								frappe.msgprint(r.message);
							}
							frm.reload_doc();
						},
					});
				}
			);
		},
		__("Webhooks")
	);

	frm.add_custom_button(
		__("View Webhook Logs"),
		() => {
			frappe.set_route("List", "Medusa Webhook Log");
		},
		__("Webhooks")
	);
}
