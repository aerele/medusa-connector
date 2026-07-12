// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medusa Webhook Log", {
	refresh(frm) {
		frm.disable_save();
		if (frm.doc.status === "Failed" || frm.doc.status === "Queued") {
			frm.add_custom_button(__("Retry"), () => {
				frappe.call({
					method: "medusa_connector.medusa_connector.doctype.medusa_webhook_log.medusa_webhook_log.retry_webhook",
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __("Re-queueing webhook…"),
					callback: (r) => {
						const m = r.message || {};
						if (m.ok) {
							frappe.show_alert({ message: m.message, indicator: "green" });
						} else {
							frappe.msgprint(m.message || __("Retry skipped"));
						}
						frm.reload_doc();
					},
				});
			}).addClass("btn-primary");
		}
	},
});
