// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medusa Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync Webhooks"), () => {
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
		});

		frm.add_custom_button(__("Regenerate Webhook Secret"), () => {
			frappe.confirm(
				__(
					"This invalidates the current Medusa subscriber secret until you re-paste it. Continue?"
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
		});
	},
});
