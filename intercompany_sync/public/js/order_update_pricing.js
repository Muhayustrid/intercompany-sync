/**
 * Order Update Pricing
 *
 * Enhances ERPNext's native erpnext.utils.update_child_items dialog for Sales Order
 * and Purchase Order:
 * - Makes UOM visible and editable in the dialog grid list view.
 * - Re-fetches native pricing on UOM, qty, and item changes.
 * - Preserves unchanged rows upon opening dialog.
 * - Guards against stale asynchronous responses via per-row tokens.
 * - Protects user manual rates from being overwritten by in-flight lookups.
 * - Prevents submission when price lookup is pending, failed, or missing.
 * - Blocks UOM changes on rows already partially received, delivered, or billed.
 * - Blocks modifying existing rows with persisted pricing rules (requires order amendment).
 * - Locks item_code for existing line rows while allowing selection on new rows.
 * - Temporarily subclasses frappe.ui.Dialog in synchronous try/finally while calling
 *   native helper to customize constructor opts before grid builds, restoring always.
 */

(function (root, factory) {
	if (typeof module === "object" && module.exports) {
		module.exports = factory();
	} else {
		root.order_update_pricing = factory();
	}
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
	"use strict";

	function calculate_rate(message, precisions) {
		const rate_prec = (precisions && precisions.rate) ?? 2;
		const rwm_prec = (precisions && precisions.rate_with_margin) ?? rate_prec;
		const disc_prec = (precisions && precisions.discount_amount) ?? 2;

		// 1. Source row locked rate: lock_source_rate sets message.rate directly (> 0)
		if (flt(message.rate) > 0) {
			return { rate: flt(message.rate, rate_prec), is_explicit: true };
		}

		let base_rate = message.blanket_order_rate != null ? message.blanket_order_rate : message.price_list_rate;

		if (base_rate == null || isNaN(base_rate)) {
			return { rate: null, is_explicit: false };
		}

		base_rate = flt(base_rate);

		// 2. Margin (Percentage or Amount) - round rate_with_margin ONLY if margin present
		let rate_with_margin = base_rate;
		if (message.margin_type === "Percentage" && message.margin_rate_or_amount) {
			rate_with_margin = flt(base_rate * (1 + flt(message.margin_rate_or_amount) / 100), rwm_prec);
		} else if (message.margin_type === "Amount" && message.margin_rate_or_amount) {
			rate_with_margin = flt(base_rate + flt(message.margin_rate_or_amount), rwm_prec);
		}

		// 3. Discount (Percentage or Amount)
		let final_rate = rate_with_margin;
		if (message.discount_percentage) {
			const discount = flt((rate_with_margin * flt(message.discount_percentage)) / 100, disc_prec);
			final_rate = rate_with_margin - discount;
		} else if (message.discount_amount) {
			final_rate = rate_with_margin - flt(message.discount_amount, disc_prec);
		}
		final_rate = flt(final_rate, rate_prec);

		// 4. Free item or 100% discount is explicit zero
		if (message.is_free_item || flt(message.discount_percentage) === 100) {
			return { rate: 0, is_explicit: true };
		}

		// Ambiguous zero without clear discount rule requires explicit confirmation
		if (final_rate === 0) {
			return { rate: 0, is_explicit: false };
		}

		// Negative rates or any non-zero rate is explicit
		return { rate: final_rate, is_explicit: true };
	}

	function validate_items_before_submit(dialog, opts) {
		const items =
			(dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.df.data) || [];
		for (let i = 0; i < items.length; i++) {
			const row = items[i];
			if (!row.item_code) continue;

			if (row.__pricing_status === "pending") {
				frappe.msgprint(__("Pricing calculation is still in progress. Please wait a moment."));
				return false;
			}
			if (row.__pricing_status === "failed") {
				frappe.msgprint(
					__("Pricing lookup failed for item {0}. Please retry or select a valid UOM.", [row.item_code])
				);
				return false;
			}
			if (row.__pricing_status === "missing" && !row.__manual_rate) {
				frappe.msgprint(
					__("No price list rate found for item {0} (Row #{1}). Please enter a rate manually.", [
						row.item_code,
						row.idx || i + 1,
					])
				);
				return false;
			}
			const conv = flt(row.conversion_factor);
			if (!Number.isFinite(conv) || conv <= 0) {
				frappe.msgprint(__("Invalid conversion factor for item {0}. Please re-select UOM.", [row.item_code || ""]));
				return false;
			}
			if (row.docname && row.__initial_item_code && row.item_code !== row.__initial_item_code) {
				frappe.msgprint(__("Item Code cannot be changed for existing row {0}.", [row.__initial_item_code]));
				return false;
			}
			if (opts && opts.frm && opts.frm.doc && opts.frm.doc.items) {
				const existing_item = opts.frm.doc.items.find((d) => d.name === (row.docname || row.name));
				if (existing_item) {
					const has_delivered = flt(existing_item.delivered_qty) > 0;
					const has_received = flt(existing_item.received_qty) > 0;
					const has_billed = flt(existing_item.billed_amt) > 0;
					if ((has_delivered || has_received || has_billed) && row.__initial_uom && row.uom !== row.__initial_uom) {
						frappe.msgprint(
							__(
								"Cannot change UOM for item {0} because it has already been partially received, delivered, or billed.",
								[row.item_code || ""]
							)
						);
						return false;
					}

					// Restrict modifying rule-bearing existing rows after submit
					if (!opts.frm.doc.ignore_pricing_rule && existing_item.pricing_rules && existing_item.pricing_rules.length > 0) {
						const qty_changed = flt(row.qty) !== flt(row.__initial_qty);
						const uom_changed = row.uom !== row.__initial_uom;
						const rate_changed = flt(row.rate) !== flt(row.__initial_rate);
						if (qty_changed || uom_changed || rate_changed) {
							frappe.msgprint(
								__(
									"Item {0} has pricing rules applied. Updating qty, UOM, or rate after submit is restricted by native pricing rule metadata; please use standard order amendment instead.",
									[row.item_code || ""]
								)
							);
							return false;
						}
					}
				}
			}
		}
		return true;
	}

	function fetch_pricing(row, trigger_field, getDialog, opts, precisions) {
		if (!row || !row.item_code) return;

		row.__pricing_token = (row.__pricing_token || 0) + 1;
		const token = row.__pricing_token;
		row.__pricing_status = "pending";

		if (trigger_field === "item_code" || trigger_field === "uom") {
			row.__manual_rate = false;
		}

		const ctx = {
			item_code: row.item_code,
			child_docname: row.docname || row.name,
			doctype: opts.frm.doc.doctype,
			name: opts.frm.doc.name,
			child_doctype: opts.frm.doc.doctype + " Item",
			company: opts.frm.doc.company,
			currency: opts.frm.doc.currency,
			price_list: opts.frm.doc.selling_price_list || opts.frm.doc.buying_price_list,
			price_list_currency: opts.frm.doc.price_list_currency,
			plc_conversion_rate: opts.frm.doc.plc_conversion_rate,
			conversion_rate: opts.frm.doc.conversion_rate,
			customer: opts.frm.doc.customer || opts.frm.doc.party_name,
			supplier: opts.frm.doc.supplier,
			is_internal_supplier: opts.frm.doc.is_internal_supplier,
			is_internal_customer: opts.frm.doc.is_internal_customer,
			transaction_date: opts.frm.doc.transaction_date || opts.frm.doc.posting_date,
			order_type: opts.frm.doc.order_type,
			is_pos: cint(opts.frm.doc.is_pos),
			is_return: cint(opts.frm.doc.is_return),
			is_subcontracted: opts.frm.doc.is_subcontracted,
			ignore_pricing_rule: opts.frm.doc.ignore_pricing_rule,
			set_warehouse: opts.frm.doc.set_warehouse,
			warehouse: row.warehouse,
			qty: flt(row.qty) || 1,
			uom: trigger_field === "item_code" ? undefined : row.uom,
			tax_category: opts.frm.doc.tax_category,
			is_old_subcontracting_flow: opts.frm.doc.is_old_subcontracting_flow,
		};

		function on_settled() {
			if (row.__pricing_token !== token) {
				return;
			}
			if (row.__pricing_status === "pending") {
				row.__pricing_status = "failed";
				const dialog = getDialog();
				if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
					dialog.fields_dict.trans_items.grid.refresh();
				}
			}
		}

		const call_opts = {
			method: "erpnext.stock.get_item_details.get_item_details",
			args: {
				doc: opts.frm.doc,
				ctx: ctx,
			},
			callback: function (r) {
				if (row.__pricing_token !== token) {
					return;
				}

				const dialog = getDialog();

				if (r.exc || !r.message) {
					row.__pricing_status = "failed";
					if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
						dialog.fields_dict.trans_items.grid.refresh();
					}
					return;
				}

				const msg = r.message;
				const conv = flt(msg.conversion_factor);

				if (!Number.isFinite(conv) || conv <= 0) {
					row.__pricing_status = "failed";
					row.conversion_factor = 0;
					if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
						dialog.fields_dict.trans_items.grid.refresh();
					}
					return;
				}

				row.conversion_factor = conv;

				if (msg.uom) {
					row.uom = msg.uom;
				}
				if (msg.item_name) {
					row.item_name = msg.item_name;
				}
				if (!row.description && msg.description) {
					row.description = msg.description;
				}
				if (msg.bom_no) {
					row.bom_no = msg.bom_no;
				}
				if (!row.docname && msg.warehouse) {
					row.warehouse = msg.warehouse;
				}

				const rateInfo = calculate_rate(msg, precisions);

				if (rateInfo.is_explicit && rateInfo.rate != null) {
					if (!row.__manual_rate) {
						row.rate = rateInfo.rate;
					}
					row.__pricing_status = "idle";
				} else {
					row.__pricing_status = row.__manual_rate ? "idle" : "missing";
				}

				if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
					dialog.fields_dict.trans_items.grid.refresh();
				}
			},
			error: function () {
				if (row.__pricing_token !== token) {
					return;
				}
				row.__pricing_status = "failed";
				const dialog = getDialog();
				if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
					dialog.fields_dict.trans_items.grid.refresh();
				}
			},
			always: on_settled,
		};

		const req = opts.frm.call ? opts.frm.call(call_opts) : frappe.call(call_opts);
		if (req && typeof req.always === "function") {
			req.always(on_settled);
		} else if (req && typeof req.finally === "function") {
			req.finally(on_settled);
		}
	}

	function enhance_dialog_opts(dialog_opts, opts, getDialog, precisions) {
		const trans_items_field = dialog_opts.fields.find((f) => f.fieldname === "trans_items");
		if (!trans_items_field || !trans_items_field.fields) return;

		const fields = trans_items_field.fields;

		const uom_field = fields.find((f) => f.fieldname === "uom");
		if (uom_field) {
			uom_field.in_list_view = 1;
			uom_field.read_only = 0;
			delete uom_field.onchange;

			uom_field.change = function () {
				const row = this.doc;
				if (!row) return;

				const existing_item =
					opts.frm.doc.items &&
					opts.frm.doc.items.find((d) => d.name === (row.docname || row.name));

				if (existing_item) {
					const has_delivered = flt(existing_item.delivered_qty) > 0;
					const has_received = flt(existing_item.received_qty) > 0;
					const has_billed = flt(existing_item.billed_amt) > 0;

					if ((has_delivered || has_received || has_billed) && row.__initial_uom && this.value !== row.__initial_uom) {
						row.uom = row.__initial_uom;
						this.value = row.__initial_uom;
						const dialog = getDialog();
						if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
							dialog.fields_dict.trans_items.grid.refresh();
						}
						frappe.msgprint(
							__(
								"Cannot change UOM for item {0} because it has already been partially received, delivered, or billed.",
								[row.item_code || ""]
							)
						);
						return;
					}
				}

				row.uom = this.value;
				fetch_pricing(row, "uom", getDialog, opts, precisions);
			};
		}

		const item_field = fields.find((f) => f.fieldname === "item_code");
		if (item_field) {
			item_field.read_only_depends_on = "eval:doc.docname";
			item_field.change = function () {
				const row = this.doc;
				if (!row) return;

				if (row.docname && row.__initial_item_code && this.value !== row.__initial_item_code) {
					row.item_code = row.__initial_item_code;
					this.value = row.__initial_item_code;
					const dialog = getDialog();
					if (dialog && dialog.fields_dict && dialog.fields_dict.trans_items && dialog.fields_dict.trans_items.grid) {
						dialog.fields_dict.trans_items.grid.refresh();
					}
					frappe.msgprint(__("Item Code cannot be changed for an existing row."));
					return;
				}

				if (!row.docname) {
					row.uom = undefined;
					row.conversion_factor = undefined;
				}

				row.item_code = this.value;
				fetch_pricing(row, "item_code", getDialog, opts, precisions);
			};
		}

		const qty_field = fields.find((f) => f.fieldname === "qty");
		if (qty_field) {
			qty_field.change = function () {
				const row = this.doc;
				if (!row) return;
				row.qty = flt(this.value);
				fetch_pricing(row, "qty", getDialog, opts, precisions);
			};
		}

		const rate_field = fields.find((f) => f.fieldname === "rate");
		if (rate_field) {
			rate_field.change = function () {
				const row = this.doc;
				if (!row) return;
				row.rate = flt(this.value);
				row.__manual_rate = true;
				if (row.__pricing_status === "missing") {
					row.__pricing_status = "idle";
				}
			};
		}
	}

	function enhance_dialog_instance(dialog, opts) {
		const trans_items_field = dialog.fields_dict && dialog.fields_dict.trans_items;
		if (trans_items_field && trans_items_field.df && trans_items_field.df.data) {
			trans_items_field.df.data.forEach((row) => {
				row.__pricing_token = 0;
				row.__pricing_status = "idle";
				row.__manual_rate = false;
				row.__initial_uom = row.uom;
				row.__initial_qty = row.qty;
				row.__initial_rate = row.rate;
				row.__initial_item_code = row.item_code;
			});
		}

		const native_primary_action = dialog.primary_action;
		dialog.primary_action = function () {
			if (!validate_items_before_submit(dialog, opts)) {
				return;
			}
			return native_primary_action.apply(this, arguments);
		};

		const native_update_items = dialog.update_items;
		if (native_update_items) {
			dialog.update_items = function () {
				if (!validate_items_before_submit(dialog, opts)) {
					return;
				}
				return native_update_items.apply(this, arguments);
			};
		}
	}

	function init() {
		if (typeof erpnext === "undefined" || !erpnext.utils || !erpnext.utils.update_child_items) {
			return false;
		}

		if (erpnext.utils.update_child_items.__order_update_pricing_wrapped) {
			return true;
		}

		const native_update_child_items = erpnext.utils.update_child_items;

		erpnext.utils.update_child_items = function (opts) {
			const doctype = opts && opts.frm && opts.frm.doc && opts.frm.doc.doctype;
			const is_target = doctype === "Sales Order" || doctype === "Purchase Order";

			if (!is_target) {
				return native_update_child_items.apply(this, arguments);
			}

			const child_meta = frappe.get_meta && frappe.get_meta(`${doctype} Item`);
			const get_precision = (fieldname) => {
				const field = child_meta && child_meta.fields && child_meta.fields.find((f) => f.fieldname === fieldname);
				return field ? field.precision : undefined;
			};
			const resolve_precision = (fieldname) => {
				const df = child_meta && child_meta.fields && child_meta.fields.find((f) => f.fieldname === fieldname);
				if (df && frappe.meta && typeof frappe.meta.get_field_precision === "function") {
					const p = frappe.meta.get_field_precision(df, opts.frm.doc);
					if (Number.isFinite(p)) return p;
				}
				if (typeof precision === "function") {
					try {
						const p = precision(fieldname, opts.frm.doc);
						if (Number.isFinite(p)) return p;
					} catch (e) {
						// ignore
					}
				}
				if (df && df.precision != null && Number.isFinite(cint(df.precision))) {
					return cint(df.precision);
				}
				if (typeof frappe !== "undefined" && frappe.defaults && typeof frappe.defaults.get_default === "function") {
					const default_prec = cint(frappe.defaults.get_default("currency_precision"));
					if (default_prec) return default_prec;
				}
				return 2;
			};
			const precisions = {
				rate: resolve_precision("rate"),
				rate_with_margin: resolve_precision("rate_with_margin"),
				margin_rate_or_amount: resolve_precision("margin_rate_or_amount"),
				discount_amount: resolve_precision("discount_amount"),
			};

			const OriginalDialog = frappe.ui.Dialog;
			let current_dialog = null;
			const getDialog = () => current_dialog;

			try {
				class OrderPricingDialog extends OriginalDialog {
					constructor(dialog_opts) {
						const trans_items_field =
							dialog_opts &&
							dialog_opts.fields &&
							dialog_opts.fields.find((f) => f.fieldname === "trans_items");

						if (trans_items_field) {
							enhance_dialog_opts(dialog_opts, opts, getDialog, precisions);

							const native_primary_action = dialog_opts.primary_action;
							dialog_opts.primary_action = function () {
								const dialog = getDialog() || this;
								if (!validate_items_before_submit(dialog, opts)) {
									return;
								}
								if (native_primary_action) {
									return native_primary_action.apply(this, arguments);
								}
							};
						}

						super(dialog_opts);

						if (trans_items_field) {
							current_dialog = this;
							enhance_dialog_instance(this, opts);
						}
					}
				}

				frappe.ui.Dialog = OrderPricingDialog;
				return native_update_child_items.apply(this, arguments);
			} finally {
				frappe.ui.Dialog = OriginalDialog;
			}
		};

		erpnext.utils.update_child_items.__order_update_pricing_wrapped = true;
		return true;
	}

	init();

	return {
		init,
		calculate_rate,
		validate_items_before_submit,
	};
});
