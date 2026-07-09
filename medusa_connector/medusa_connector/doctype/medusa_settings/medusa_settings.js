// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medusa Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "medusa_connector.medusa_connector.doctype.medusa_settings.medusa_settings.test_connection",
				freeze: true,
				freeze_message: __("Testing connection…"),
				callback: (r) => {
					if (r.message) {
						frappe.show_alert({
							message: r.message.message || __("Done"),
							indicator: r.message.status === "Connected" ? "green" : "red",
						});
					}
					frm.reload_doc();
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
