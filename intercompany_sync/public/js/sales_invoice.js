frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		// Linked Purchase Invoice is cancelled by server cascade, not client cancel_all.
		ensure_pi_ignored(frm);
		// ERPNext onload re-assigns this list and can land after refresh on fresh
		// loads, so enforce it where Cancel actually reads it.
		if (!frm.__ic_savecancel_wrapped) {
			frm.__ic_savecancel_wrapped = true;
			const orig_savecancel = frm.savecancel.bind(frm);
			frm.savecancel = (...args) => {
				ensure_pi_ignored(frm);
				return orig_savecancel(...args);
			};
		}
	},
});

function ensure_pi_ignored(frm) {
	frm.ignore_doctypes_on_cancel_all = frm.ignore_doctypes_on_cancel_all || [];
	if (!frm.ignore_doctypes_on_cancel_all.includes("Purchase Invoice")) {
		frm.ignore_doctypes_on_cancel_all.push("Purchase Invoice");
	}
}
