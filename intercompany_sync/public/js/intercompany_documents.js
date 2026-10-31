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

	const ALLOWED_METADATA_KEYS = new Set([
		"doctype",
		"name",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"flags",
	]);

	const ALLOWED_ONE_DEFAULTS = new Set([
		"qty",
		"conversion_factor",
		"reserve_stock",
	]);

	const ALLOWED_ZERO_STRING_FIELDS = new Set([
		"qty",
		"conversion_factor",
		"reserve_stock",
		"is_free_item",
		"delivered_by_supplier",
		"page_break",
		"against_blanket_order",
		"grant_commission",
		"stock_reserved_qty",
		"is_stock_item",
		"ensure_delivery_based_on_produced_serial_no",
		"received_qty",
		"rejected_qty",
		"retain_sample",
		"has_item_scanned",
		"allow_zero_valuation_rate",
		"rate",
		"amount",
		"stock_qty",
		"price_list_rate",
		"base_rate",
		"base_amount",
		"net_rate",
		"net_amount",
		"base_net_rate",
		"base_net_amount",
		"discount_percentage",
		"discount_amount",
	]);

	function is_virgin_placeholder(row) {
		if (!row || typeof row !== "object") return false;
		if (row.__unedited === false) return false;

		for (const key of Object.keys(row)) {
			if (key.startsWith("__") || key.startsWith("_")) {
				continue;
			}

			if (ALLOWED_METADATA_KEYS.has(key)) {
				if (key === "docstatus" && row[key] !== 0 && row[key] !== "0") {
					return false;
				}
				continue;
			}

			const val = row[key];

			if (val === undefined || val === null) {
				continue;
			}

			if (val === false) {
				continue;
			}

			if (val === true) {
				if (ALLOWED_ONE_DEFAULTS.has(key)) {
					continue;
				}
				return false;
			}

			// Numeric 0 is allowed on any field as empty/default
			if (val === 0 || val === 0.0) {
				continue;
			}

			// Numeric 1 is allowed only for documented native defaults
			if (val === 1 || val === 1.0) {
				if (ALLOWED_ONE_DEFAULTS.has(key)) {
					continue;
				}
				return false;
			}

			// Check legitimate numeric/flag string defaults BEFORE generic string rejection
			if (val === "1" && ALLOWED_ONE_DEFAULTS.has(key)) {
				continue;
			}
			if (val === "0" && ALLOWED_ZERO_STRING_FIELDS.has(key)) {
				continue;
			}

			// Generic string check: empty string is untouched; any other string (including batch_no: "0") is occupied
			if (typeof val === "string") {
				if (val.trim() === "") {
					continue;
				}
				return false;
			}

			return false;
		}

		return true;
	}

	function is_target_occupied(doc) {
		const items = doc.items || [];
		if (items.length === 0) return false;
		if (items.length === 1 && is_virgin_placeholder(items[0])) {
			return false;
		}
		return true;
	}

	function set_dialog_status(dialog, status, message, retry_fn) {
		dialog.retry_action = retry_fn || null;
		let html = "";
		if (status === "loading") {
			html = `<div class="text-muted small my-2">${message}</div>`;
		} else if (status === "empty") {
			html = `<div class="text-muted small my-2">${message}</div>`;
		} else if (status === "error") {
			html = `<div class="text-danger small my-2">${message} <a href="#" class="ic-retry-btn text-primary">${__("Coba Lagi")}</a></div>`;
		}

		dialog.set_df_property("status_html", "options", html);
		const status_field = dialog.fields_dict?.status_html;
		if (status_field) {
			if (typeof status_field.html === "function") {
				status_field.html(html);
			} else if (status_field.$wrapper) {
				status_field.$wrapper.html(html);
			}
			if (status_field.$wrapper?.find) {
				status_field.$wrapper.find(".ic-retry-btn").on("click", function (e) {
					e.preventDefault();
					if (typeof dialog.retry_action === "function") {
						dialog.retry_action();
					}
				});
			}
		}
	}

	function update_source_options(dialog, options) {
		dialog.set_df_property("source_name", "options", options);
		const field = dialog.fields_dict?.source_name;
		if (field) {
			if (typeof field.set_data === "function") {
				field.set_data(options);
			}
			if (typeof field.refresh === "function") {
				field.refresh();
			}
		}
	}

	function open_source_picker(frm, source_doctype) {
		if (!frm.doc.company) {
			frappe.msgprint(__("Harap pilih Company terlebih dahulu."));
			return;
		}

		if (is_target_occupied(frm.doc)) {
			frappe.msgprint({
				title: __("Target Tidak Kosong"),
				message: __(
					"Dokumen target sudah berisi item atau modifikasi baris. Harap kosongkan tabel item sebelum memetakan dokumen sumber."
				),
				indicator: "red",
			});
			return;
		}

		let req_seq = 0;

		const dialog = new frappe.ui.Dialog({
			title: __("Pilih {0}", [__(source_doctype)]),
			fields: [
				{
					fieldname: "search_text",
					fieldtype: "Data",
					label: __("Cari"),
					placeholder: __("Cari No. Dokumen atau Company..."),
					onchange: function () {
						fetch_sources(dialog.get_value("search_text"));
					},
				},
				{
					fieldname: "source_name",
					fieldtype: "Select",
					label: __(source_doctype),
					reqd: 1,
					options: [],
				},
				{
					fieldname: "status_html",
					fieldtype: "HTML",
				},
			],
			primary_action_label: __("Get Items"),
			primary_action: function (values) {
				const selected_source = values.source_name || dialog.get_value("source_name");
				if (!selected_source) {
					frappe.msgprint(__("Harap pilih dokumen sumber terlebih dahulu."));
					return;
				}

				if (is_target_occupied(frm.doc)) {
					frappe.msgprint({
						title: __("Target Tidak Kosong"),
						message: __(
							"Dokumen target sudah berisi item atau modifikasi baris. Harap kosongkan tabel item sebelum memetakan dokumen sumber."
						),
						indicator: "red",
					});
					return;
				}

				// Clone payload and prune only untouched native placeholder row
				const target_payload = JSON.parse(JSON.stringify(frm.doc));
				if (
					target_payload.items &&
					target_payload.items.length === 1 &&
					is_virgin_placeholder(target_payload.items[0])
				) {
					target_payload.items = [];
				}

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
						dialog.hide();
					},
				});
			},
		});

		function fetch_sources(txt) {
			const current_seq = ++req_seq;
			set_dialog_status(dialog, "loading", __("Memuat dokumen sumber..."));
			update_source_options(dialog, []);

			const target_party = frm.doctype === "Sales Order" ? frm.doc.customer : frm.doc.supplier;

			frappe.call({
				method: "intercompany_sync.mapping.get_sources",
				args: {
					source_doctype: source_doctype,
					target_company: frm.doc.company,
					target_party: target_party || null,
					txt: txt || "",
					start: 0,
					page_length: 20,
				},
				callback: function (r) {
					if (current_seq !== req_seq) return;

					if (r.exc || !r.message) {
						set_dialog_status(
							dialog,
							"error",
							__("Gagal memuat dokumen sumber."),
							() => fetch_sources(txt)
						);
						return;
					}

					const sources = r.message;
					if (!sources || sources.length === 0) {
						set_dialog_status(dialog, "empty", __("Tidak ada dokumen sumber yang tersedia."));
						update_source_options(dialog, []);
						return;
					}

					set_dialog_status(dialog, "success", "");
					const options = [{ value: "", label: __("-- Pilih Dokumen Sumber --") }];
					for (const s of sources) {
						const party = s.supplier || s.customer || "";
						const total = s.grand_total ? ` - ${s.currency || ""} ${s.grand_total}` : "";
						options.push({
							value: s.name,
							label: `${s.name} (${s.company}${party ? " / " + party : ""}${total})`,
						});
					}
					update_source_options(dialog, options);
				},
				error: function () {
					if (current_seq !== req_seq) return;
					set_dialog_status(
						dialog,
						"error",
						__("Gagal memuat dokumen sumber."),
						() => fetch_sources(txt)
					);
				},
			});
		}

		dialog.show();
		fetch_sources("");
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
