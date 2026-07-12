// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.listview_settings["Medusa Webhook Log"] = {
	get_indicator(doc) {
		const colors = {
			Processed: "green",
			Failed: "red",
			Queued: "orange",
			Rejected: "red",
			Received: "blue",
			Verified: "blue",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
	onload(listview) {
		listview.page.add_actions_menu_item(__("Retry Failed"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Select one or more logs"));
				return;
			}
			frappe.call({
				method: "medusa_connector.medusa_connector.doctype.medusa_webhook_log.medusa_webhook_log.bulk_retry",
				args: { names },
				freeze: true,
				callback: () => {
					frappe.show_alert({ message: __("Retry queued"), indicator: "green" });
					listview.refresh();
				},
			});
		});
	},
};
