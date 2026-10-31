/**
 * Shared client actions for intercompany Sales Order, Purchase Order, Delivery Note, and Purchase Receipt.
 */
(function () {
	const root = typeof window !== "undefined" ? window : globalThis;
	root.intercompany_sync = root.intercompany_sync || {};
	const ic = root.intercompany_sync;
	ic.registered_doctypes = ic.registered_doctypes || {};

	function register_doctype(doctype, handlers) {
		if (ic.registered_doctypes[doctype]) return;
		ic.registered_doctypes[doctype] = true;
		frappe.ui.form.on(doctype, handlers);
	}

	function open_source_picker(frm, source_doctype) {
		if (!frm.doc.company) {
			frappe.msgprint(__("Harap pilih Company terlebih dahulu."));
			return;
		}

		const is_po = source_doctype === "Purchase Order";
		const target_party = frm.doctype === "Sales Order" ? frm.doc.customer : frm.doc.supplier;

		// Same native picker as ERPNext "Get Items From"; eligibility comes from search_sources
		const picker = new frappe.ui.form.MultiSelectDialog({
			doctype: source_doctype,
			target: frm,
			setters: [
				{ label: __("Company"), fieldname: "company", fieldtype: "Link", options: "Company" },
				is_po
					? { label: __("Date"), fieldname: "transaction_date", fieldtype: "Date" }
					: { label: __("Posting Date"), fieldname: "posting_date", fieldtype: "Date" },
			],
			add_filters_group: 1,
			get_query() {
				return {
					query: "intercompany_sync.mapping.search_sources",
					filters: { target_company: frm.doc.company, target_party: target_party || "" },
				};
			},
			action(selections) {
				// The mapper links the target to one source document (inter_company_reference)
				if (!selections || selections.length !== 1) {
					frappe.msgprint(__("Pilih tepat satu {0}.", [__(source_doctype)]));
					return;
				}
				const selected_source = selections[0];

				// Like native Get Items, the mapped result replaces the current item table
				const target_payload = { ...frm.doc, items: [] };

				// Snapshot full document before RPC dispatch to prevent overwriting delayed changes
				const dispatch_snapshot = JSON.stringify(frm.doc);

				const method =
					source_doctype === "Purchase Order"
						? "intercompany_sync.mapping.make_sales_order"
						: "intercompany_sync.mapping.make_purchase_receipt";

				frappe.call({
					method: method,
					args: {
						source_name: selected_source,
						target_doc: JSON.stringify(target_payload),
					},
					freeze: true,
					freeze_message: __("Memetakan dokumen..."),
					callback: function (r) {
						if (r.exc || !r.message) return;

						// Reject if any header, item, or status changed programmatically during RPC
						const current_snapshot = JSON.stringify(frm.doc);
						if (current_snapshot !== dispatch_snapshot) {
							frappe.msgprint({
								title: __("Target Berubah"),
								message: __(
									"Dokumen target telah berubah saat permintaan berlangsung. Hasil pemetaan dibatalkan."
								),
								indicator: "orange",
							});
							return;
						}

						frappe.model.sync(r.message);
						frm.dirty();
						frm.refresh();
						picker.dialog.hide();
					},
				});
			},
		});
	}

	function adapt_dashboard_open_document_list(frm) {
		if (!frm || !frm.dashboard || frm.dashboard._ic_open_adapted) return;
		const original_open = frm.dashboard.open_document_list;
		if (typeof original_open !== "function") return;

		frm.dashboard.open_document_list = function ($link, show_open) {
			const doctype = typeof $link?.attr === "function" ? $link.attr("data-doctype") : null;
			if (doctype === frm.doctype) {
				return original_open.call(this, $link, show_open);
			}

			const is_internal =
				this.internal_links_found &&
				this.internal_links_found.find((d) => d.doctype === doctype);

			if (is_internal) {
				let target_names = [];
				if (show_open) {
					target_names = is_internal.open_names || [];
				} else {
					if (Array.isArray(is_internal.names)) {
						target_names = is_internal.names;
					} else {
						const raw_names = typeof $link?.attr === "function" ? $link.attr("data-names") : null;
						if (typeof raw_names === "string" && raw_names.trim().length > 0) {
							target_names = raw_names
								.split(",")
								.map((s) => s.trim())
								.filter(Boolean);
						} else if (Array.isArray(raw_names)) {
							target_names = raw_names;
						}
					}
				}

				if (target_names.length) {
					frappe.route_options = { name: ["in", target_names] };
					frappe.set_route("List", doctype, "List");
				}
				// Zero names never opens unrestricted List
				return false;
			}

			return original_open.call(this, $link, show_open);
		};
		frm.dashboard._ic_open_adapted = true;
	}

	// Register Sales Order actions
	register_doctype("Sales Order", {
		refresh(frm) {
			adapt_dashboard_open_document_list(frm);
			if (frm.doc.docstatus === 0 && frappe.model.can_read("Purchase Order")) {
				frm.remove_custom_button(__("Inter Company Purchase Order"), __("Get Items From"));
				frm.add_custom_button(
					__("Inter Company Purchase Order"),
					function () {
						open_source_picker(frm, "Purchase Order");
					},
					__("Get Items From")
				);
			}
		},
	});

	// Register Purchase Order actions (safe no-op handler)
	register_doctype("Purchase Order", {
		refresh(frm) {
			adapt_dashboard_open_document_list(frm);
		},
	});

	// Register Purchase Receipt actions
	register_doctype("Purchase Receipt", {
		refresh(frm) {
			adapt_dashboard_open_document_list(frm);
			if (frm.doc.docstatus === 0 && frappe.model.can_read("Delivery Note")) {
				frm.remove_custom_button(__("Inter Company Delivery Note"), __("Get Items From"));
				frm.add_custom_button(
					__("Inter Company Delivery Note"),
					function () {
						open_source_picker(frm, "Delivery Note");
					},
					__("Get Items From")
				);
			}
		},
	});

	// Register Delivery Note actions
	register_doctype("Delivery Note", {
		refresh(frm) {
			adapt_dashboard_open_document_list(frm);
			const button_label =
				frm.doc.company === frm.doc.represents_company
					? "Internal Purchase Receipt"
					: "Inter Company Purchase Receipt";

			// Always remove native button if present to prevent broken handler or duplicate button
			frm.remove_custom_button(__(button_label), __("Create"));

			// Only add wrapper if submitted, internal customer, not return, not closed, and can create PR
			if (
				frm.doc.docstatus === 1 &&
				frm.doc.is_internal_customer &&
				!frm.doc.is_return &&
				frm.doc.status !== "Closed" &&
				frappe.model.can_create("Purchase Receipt")
			) {
				frm.add_custom_button(
					__(button_label),
					function () {
						frappe.model.open_mapped_doc({
							method: "intercompany_sync.mapping.make_purchase_receipt",
							frm: frm,
						});
					},
					__("Create")
				);
			}
		},
	});
})();
