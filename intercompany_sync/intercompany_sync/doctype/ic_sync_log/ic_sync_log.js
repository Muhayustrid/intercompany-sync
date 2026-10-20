// Copyright (c) 2026, Rotiri Project and contributors
// License: MIT

frappe.ui.form.on("IC Sync Log", {
	refresh(frm) {
		if (frm.doc.status === "Failed" && !frm.is_new()) {
			frm.add_custom_button(__("Retry"), () => {
				frappe.call({
					method: "intercompany_sync.intercompany_sync.doctype.ic_sync_log.ic_sync_log.retry",
					args: {
						name: frm.doc.name,
					},
					freeze: true,
					freeze_message: __("Retrying..."),
					callback(r) {
						if (!r.exc) {
							frm.reload_doc();
						}
					},
				});
			});
		}
	},
});
