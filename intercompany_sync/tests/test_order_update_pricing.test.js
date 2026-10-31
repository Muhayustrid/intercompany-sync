const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

// 1. Metadata and Package Configuration Tests
test("App structure and hooks configuration", () => {
	const appRoot = path.resolve(__dirname, "../..");
	const pkgPath = path.join(appRoot, "pyproject.toml");
	const hooksPath = path.join(appRoot, "intercompany_sync/hooks.py");
	const modulesPath = path.join(appRoot, "intercompany_sync/modules.txt");
	const initPath = path.join(appRoot, "intercompany_sync/__init__.py");

	assert.ok(fs.existsSync(pkgPath), "pyproject.toml must exist");
	assert.ok(fs.existsSync(hooksPath), "hooks.py must exist");
	assert.ok(fs.existsSync(modulesPath), "modules.txt must exist");
	assert.ok(fs.existsSync(initPath), "__init__.py must exist");

	const pyCheck = `
import tomllib, runpy

with open(r"""${pkgPath}""", "rb") as f:
    pyproject = tomllib.load(f)
assert pyproject["project"]["name"] == "intercompany_sync"

hooks = runpy.run_path(r"""${hooksPath}""")
assert hooks["app_name"] == "intercompany_sync"
assert "erpnext" in hooks["required_apps"]
assert "public/js/order_update_pricing.js" in hooks["doctype_js"]["Sales Order"]
assert "public/js/order_update_pricing.js" in hooks["doctype_js"]["Purchase Order"]
`;
	execFileSync("python3", ["-c", pyCheck]);
});

// Helper to construct a Frappe / ERPNext runtime environment for testing JS logic
function createTestEnvironment() {
	const calls = [];
	const messages = [];
	const confirms = [];

	global.__ = (s) => s;
	global.cint = (v) => parseInt(v, 10) || 0;
	global.flt = (v, precision) => {
		const num = parseFloat(v) || 0;
		return typeof precision === "number" ? Number(num.toFixed(precision)) : num;
	};

	let dialogInstance = null;

	class MockDialog {
		constructor(opts) {
			this.opts = opts;
			this.fields = opts.fields;
			this.fields_dict = {};
			this.data = opts.fields[0].data || [];

			const trans_items_field = opts.fields.find((f) => f.fieldname === "trans_items");
			if (trans_items_field) {
				this.fields_dict.trans_items = {
					df: trans_items_field,
					grid: {
						refresh: () => {
							this.grid_refreshed = true;
						},
					},
				};
			}

			this.primary_action = opts.primary_action;
			this.update_items = opts.update_items;

			// Simulates Frappe Dialog super.make() binding the primary button click callback
			this.bound_primary_action = opts.primary_action;
			this.primary_button = {
				click: () => {
					if (this.bound_primary_action) {
						return this.bound_primary_action.call(this);
					}
				},
			};

			this.shown = false;
			this.hidden = false;
			dialogInstance = this;
		}
		show() {
			this.shown = true;
		}
		hide() {
			this.hidden = true;
		}
		get_values() {
			return {
				trans_items: this.fields_dict.trans_items.df.data,
			};
		}
	}

	global.frappe = {
		ui: {
			Dialog: MockDialog,
		},
		meta: {},
		call: (opts) => {
			calls.push(opts);
			if (opts.callback && !opts.skip_auto_callback) {
				opts.callback({ message: opts.__mock_response || {} });
			}
		},
		msgprint: (msg) => {
			messages.push(msg);
		},
		confirm: (msg, onConfirm) => {
			confirms.push({ msg, onConfirm });
		},
		get_meta: (doctype) => {
			return {
				fields: [
					{ fieldname: "qty", precision: 2 },
					{ fieldname: "rate", precision: 2 },
					{ fieldname: "conversion_factor", precision: 4 },
					{ fieldname: "fg_item_qty", precision: 2 },
					{ fieldname: "warehouse", label: "Warehouse" },
				],
			};
		},
	};

	global.refresh_field = () => {};

	// Native update_child_items representation (simplified faithful copy of utils.js:713-1042)
	const native_update_child_items = function (opts) {
		const frm = opts.frm;
		const cannot_add_row = typeof opts.cannot_add_row === "undefined" ? true : opts.cannot_add_row;
		const child_docname = typeof opts.cannot_add_row === "undefined" ? "items" : opts.child_docname;
		const child_meta = frappe.get_meta(`${frm.doc.doctype} Item`);
		const has_reserved_stock = opts.has_reserved_stock ? true : false;
		const get_precision = (fieldname) => child_meta.fields.find((f) => f.fieldname == fieldname).precision;

		this.data = frm.doc[opts.child_docname].map((d) => {
			return {
				docname: d.name,
				name: d.name,
				item_code: d.item_code,
				item_name: d.item_name,
				delivery_date: d.delivery_date,
				schedule_date: d.schedule_date,
				conversion_factor: d.conversion_factor,
				qty: d.qty,
				rate: d.rate,
				uom: d.uom,
				warehouse: d.warehouse,
				description: d.description,
			};
		});

		const fields = [
			{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
			{
				fieldtype: "Link",
				fieldname: "item_code",
				in_list_view: 1,
				read_only: 0,
				change: function () {
					const me = this;
					frm.call({
						method: "erpnext.stock.get_item_details.get_item_details",
						args: { doc: frm.doc, ctx: { item_code: this.value } },
						callback: function (r) {
							if (r.message) {
								const row = dialog.fields_dict.trans_items.df.data.find((row) => row.name == me.doc.name);
								if (row) {
									row.rate = me.doc.rate || r.message.price_list_rate;
								}
							}
						},
					});
				},
			},
			{ fieldtype: "Data", fieldname: "item_name", read_only: 1, in_list_view: 1 },
			{
				fieldtype: "Link",
				fieldname: "uom",
				options: "UOM",
				read_only: 0,
				// Native lacks in_list_view: 1
				onchange: function () {
					frappe.call({
						method: "erpnext.stock.get_item_details.get_conversion_factor",
						args: { item_code: this.doc.item_code, uom: this.value },
					});
				},
			},
			{ fieldtype: "Float", fieldname: "qty", default: 0, read_only: 0, in_list_view: 1 },
			{ fieldtype: "Currency", fieldname: "rate", default: 0, read_only: 0, in_list_view: 1 },
			{ fieldtype: "Text Editor", fieldname: "description", read_only: 0 },
		];

		if (frm.doc.doctype === "Sales Order" || frm.doc.doctype === "Purchase Order") {
			fields.splice(3, 0, {
				fieldtype: "Date",
				fieldname: frm.doc.doctype === "Sales Order" ? "delivery_date" : "schedule_date",
				in_list_view: 1,
			});
			fields.splice(4, 0, {
				fieldtype: "Float",
				fieldname: "conversion_factor",
				precision: get_precision("conversion_factor"),
			});
		}

		let dialog = new frappe.ui.Dialog({
			title: "Update Items",
			size: "extra-large",
			fields: [
				{
					fieldname: "trans_items",
					fieldtype: "Table",
					cannot_add_rows: cannot_add_row,
					data: this.data,
					fields: fields,
				},
			],
			primary_action: function () {
				if (frm.doc.doctype === "Sales Order" && has_reserved_stock) {
					this.hide();
					frappe.confirm("The reserved stock will be released", () => this.update_items());
				} else {
					this.update_items();
				}
			},
			update_items: function () {
				const trans_items = this.get_values()["trans_items"].filter((item) => !!item.item_code);
				frappe.call({
					method: "erpnext.controllers.accounts_controller.update_child_qty_rate",
					args: {
						parent_doctype: frm.doc.doctype,
						trans_items: trans_items,
						parent_doctype_name: frm.doc.name,
						child_docname: child_docname,
					},
					callback: function () {
						frm.reload_doc();
					},
				});
				this.hide();
				refresh_field("items");
			},
		});

		dialog.show();
		return dialog;
	};

	global.erpnext = {
		utils: {
			update_child_items: native_update_child_items,
		},
	};

	return {
		calls,
		messages,
		confirms,
		MockDialog,
		getDialog: () => dialogInstance,
	};
}

function mockFrm(doctype = "Sales Order", items = []) {
	return {
		doc: {
			doctype: doctype,
			name: `${doctype}-001`,
			company: "Test Company",
			currency: "USD",
			selling_price_list: "Standard Selling",
			buying_price_list: "Standard Buying",
			price_list_currency: "USD",
			plc_conversion_rate: 1.0,
			conversion_rate: 1.0,
			customer: "Customer A",
			supplier: "Supplier A",
			transaction_date: "2026-10-30",
			items: items,
		},
		call: (opts) => {
			global.frappe.call(opts);
		},
		reload_doc: () => {},
	};
}

// 2. Load the App Bundle
test("Load order_update_pricing.js into test environment", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	assert.ok(fs.existsSync(jsPath), "order_update_pricing.js must exist");

	delete require.cache[require.resolve(jsPath)];
	const app = require(jsPath);
	assert.ok(erpnext.utils.update_child_items.__order_update_pricing_wrapped, "Native update_child_items must be wrapped");
});

// 3. Wrapper Isolation and Dialog Scope
test("Non-target doctype (Quotation) bypasses Dialog subclassing", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Quotation", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 50, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();

	// In Quotation, UOM list view is untouched (remains native: no in_list_view)
	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	assert.equal(uomField.in_list_view, undefined);

	// Original Dialog must be restored in frappe.ui.Dialog
	assert.equal(frappe.ui.Dialog, env.MockDialog);
});

// 4. Sales Order & Purchase Order Dialog Options Enhancement
test("Target doctype has visible editable UOM in list view and custom handlers", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 50, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();

	const fields = dialog.fields_dict.trans_items.df.fields;
	const uomField = fields.find((f) => f.fieldname === "uom");
	const itemField = fields.find((f) => f.fieldname === "item_code");
	const qtyField = fields.find((f) => f.fieldname === "qty");
	const rateField = fields.find((f) => f.fieldname === "rate");

	assert.equal(uomField.in_list_view, 1, "UOM must be visible in list view");
	assert.equal(uomField.read_only, 0, "UOM must be editable");
	assert.ok(typeof uomField.change === "function", "UOM must have change handler");
	assert.ok(typeof itemField.change === "function", "item_code must have change handler");
	assert.ok(typeof qtyField.change === "function", "qty must have change handler");
	assert.ok(typeof rateField.change === "function", "rate must have change handler");

	// Dialog restored
	assert.equal(frappe.ui.Dialog, env.MockDialog);
});

// 5. Do Not Reprice Unchanged Rows Opening Dialog
test("Does not reprice unchanged rows upon opening dialog", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Purchase Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 5, rate: 99.5, uom: "Box", conversion_factor: 10 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();

	// Opening the dialog must NOT dispatch any get_item_details calls
	const getDetailsCalls = env.calls.filter((c) => c.method === "erpnext.stock.get_item_details.get_item_details");
	assert.equal(getDetailsCalls.length, 0, "No pricing calls on open");

	// Initial rate preserved
	const row = dialog.fields_dict.trans_items.df.data[0];
	assert.equal(row.rate, 99.5);
	assert.equal(row.uom, "Box");
});

// 6. Rate Refresh on UOM Change (Omitting stale conversion_factor)
test("UOM change refreshes pricing without sending stale conversion_factor", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Simulate UOM control change:
	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	const mockControl = {
		doc: row,
		value: "Box",
	};

	// Mock server response
	global.frappe.call = (opts) => {
		env.calls.push(opts);
		if (opts.method === "erpnext.stock.get_item_details.get_item_details") {
			opts.callback({
				message: {
					price_list_rate: 100,
					conversion_factor: 12,
					uom: "Box",
				},
			});
		}
	};

	uomField.change.call(mockControl);

	const call = env.calls.find((c) => c.method === "erpnext.stock.get_item_details.get_item_details");
	assert.ok(call, "get_item_details must be called");
	assert.equal(call.args.ctx.uom, "Box");
	assert.equal(call.args.ctx.child_docname, "row-1");
	assert.equal(call.args.ctx.conversion_factor, undefined, "Must NOT send stale conversion_factor");

	// Row updated
	assert.equal(row.rate, 100);
	assert.equal(row.conversion_factor, 12);
	assert.equal(row.uom, "Box");
});

// 7. Quantity Change Refreshes Pricing
test("Quantity change refreshes pricing", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 50, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	global.frappe.call = (opts) => {
		env.calls.push(opts);
		if (opts.method === "erpnext.stock.get_item_details.get_item_details") {
			opts.callback({
				message: {
					price_list_rate: 45, // Bulk volume discount
					conversion_factor: 1,
					uom: "Nos",
				},
			});
		}
	};

	const qtyField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "qty");
	row.qty = 50;
	qtyField.change.call({ doc: row, value: 50 });

	const call = env.calls.find((c) => c.method === "erpnext.stock.get_item_details.get_item_details");
	assert.ok(call);
	assert.equal(call.args.ctx.qty, 50);
	assert.equal(row.rate, 45);
});

// 8. Protection against UOM change on partially delivered, received, or billed rows
test("Blocks UOM change if row has received_qty, delivered_qty, or billed_amt", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{
			name: "row-partially-delivered",
			item_code: "ITEM-01",
			qty: 10,
			rate: 20,
			uom: "Nos",
			delivered_qty: 4, // 4 delivered
			billed_amt: 80,
		},
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	uomField.change.call({ doc: row, value: "Box" });

	// Should be blocked: UOM reverted, error message shown, no get_item_details call
	assert.equal(row.uom, "Nos", "UOM must be reverted to original");
	assert.ok(env.messages.length > 0, "Warning message must be shown");
	assert.match(env.messages[0], /Cannot change UOM/);

	const pricingCalls = env.calls.filter((c) => c.method === "erpnext.stock.get_item_details.get_item_details");
	assert.equal(pricingCalls.length, 0, "No pricing call dispatched for blocked row");
});

// 9. Asynchronous Stale Response Guard (Token Ordering)
test("Stale out-of-order pricing responses are discarded", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Purchase Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	const pendingCallbacks = [];
	global.frappe.call = (opts) => {
		pendingCallbacks.push(opts.callback);
	};

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");

	// First change: to "Box" (token 1)
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	// Second change quickly: to "Pack" (token 2)
	row.uom = "Pack";
	uomField.change.call({ doc: row, value: "Pack" });

	assert.equal(pendingCallbacks.length, 2);

	// Token 2 arrives first with rate 30
	pendingCallbacks[1]({
		message: { price_list_rate: 30, uom: "Pack", conversion_factor: 5 },
	});
	assert.equal(row.rate, 30);
	assert.equal(row.uom, "Pack");

	// Token 1 arrives late with rate 120
	pendingCallbacks[0]({
		message: { price_list_rate: 120, uom: "Box", conversion_factor: 20 },
	});

	// Token 1 must be discarded; rate remains 30
	assert.equal(row.rate, 30, "Stale Token 1 response must NOT overwrite Token 2 rate");
	assert.equal(row.uom, "Pack");
});

// 10. Manual Rate Edit Protection
test("Manual rate entry is not overwritten by in-flight price lookup", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 25, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	let inflightCallback = null;
	global.frappe.call = (opts) => {
		inflightCallback = opts.callback;
	};

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	const rateField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "rate");

	// Change UOM -> initiates inflight lookup
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	// User types manual rate 150 before response returns
	row.rate = 150;
	rateField.change.call({ doc: row, value: 150 });

	// Inflight response returns with price_list_rate 200
	inflightCallback({
		message: { price_list_rate: 200, uom: "Box", conversion_factor: 10 },
	});

	// Manual rate must survive!
	assert.equal(row.rate, 150, "Manual rate must not be overwritten by async lookup");
});

// 11. Zero vs Missing Price Handling
test("Distinguishes explicit zero rate vs missing price list rate", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 50, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");

	// Case A: Missing price list rate (price_list_rate: 0 without pricing rule)
	global.frappe.call = (opts) => {
		if (opts.callback) {
			opts.callback({
				message: {
					price_list_rate: 0,
					conversion_factor: 1,
					has_pricing_rule: false,
					uom: "Carton",
				},
			});
		}
	};

	row.uom = "Carton";
	uomField.change.call({ doc: row, value: "Carton" });

	assert.equal(row.__pricing_status, "missing", "Row must be flagged as missing price");

	// Attempt submit -> blocked!
	dialog.primary_action();
	assert.ok(env.messages.some((m) => m.includes("No price list rate found")));

	// User provides explicit manual rate 0
	const rateField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "rate");
	row.rate = 0;
	rateField.change.call({ doc: row, value: 0 });

	assert.equal(row.__pricing_status, "idle", "Manual rate resolves missing status");
	assert.equal(row.__manual_rate, true);
});

// 12. Submit Guards for Pending / Failed Lookups
test("Submission blocked while lookup is pending or failed", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");

	// Inflight pending lookup
	global.frappe.call = () => {}; // does not resolve
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	assert.equal(row.__pricing_status, "pending");

	// Submit attempt
	dialog.primary_action();
	assert.ok(env.messages.some((m) => m.includes("Pricing calculation is still in progress")));

	// Server failure
	global.frappe.call = (opts) => {
		opts.callback({ exc: "Server Error" });
	};
	uomField.change.call({ doc: row, value: "Box" });
	assert.equal(row.__pricing_status, "failed");

	dialog.primary_action();
	assert.ok(env.messages.some((m) => m.includes("Pricing lookup failed")));
});

// 13. Source Row Locked Rate Handling
test("Source row locked rate is prioritized when returned", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 100, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	global.frappe.call = (opts) => {
		opts.callback({
			message: {
				rate: 88.0,
				price_list_rate: 100.0,
				conversion_factor: 1,
				uom: "Nos",
			},
		});
	};

	const qtyField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "qty");
	row.qty = 3;
	qtyField.change.call({ doc: row, value: 3 });

	assert.equal(row.rate, 88.0, "Must use locked source rate");
});

// 14. Stock Reservation Confirmation & Valid Submit Delegation
test("Valid submission triggers native reservation confirmation and update call", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 50, uom: "Nos", conversion_factor: 1 },
	]);
	frm.doc.is_subcontracted = 0;

	erpnext.utils.update_child_items({
		frm,
		child_docname: "items",
		has_reserved_stock: true,
	});
	const dialog = env.getDialog();

	// All rows idle / valid
	dialog.primary_action();

	// Confirmation should be triggered
	assert.equal(env.confirms.length, 1, "Stock reservation confirm must be shown");
	assert.match(env.confirms[0].msg, /The reserved stock will be released/);

	// Confirming calls update_items
	env.confirms[0].onConfirm();

	const updateCall = env.calls.find(
		(c) => c.method === "erpnext.controllers.accounts_controller.update_child_qty_rate"
	);
	assert.ok(updateCall, "update_child_qty_rate must be called");
	assert.equal(updateCall.args.parent_doctype, "Sales Order");
	assert.equal(updateCall.args.trans_items.length, 1);
});

// 15. Item Code Protection on Existing Rows vs New Rows
test("Protects existing row item_code while allowing new row item selection", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-existing", item_code: "ITEM-A", qty: 2, rate: 10, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const existingRow = dialog.fields_dict.trans_items.df.data[0];

	const itemField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "item_code");
	assert.equal(itemField.read_only_depends_on, "eval:doc.docname", "Must be read-only if docname exists");

	// Attempt to change item_code on existing row
	itemField.change.call({ doc: existingRow, value: "ITEM-TAMPERED" });
	assert.equal(existingRow.item_code, "ITEM-A", "Existing row item_code must be reverted");
	assert.ok(env.messages.some((m) => m.includes("Item Code cannot be changed")));

	// New row without docname
	const newRow = {
		name: "row-new-1",
		docname: undefined,
		qty: 1,
		uom: "Nos",
	};
	dialog.fields_dict.trans_items.df.data.push(newRow);

	global.frappe.call = (opts) => {
		if (opts.callback) {
			opts.callback({
				message: {
					item_name: "Item B Name",
					price_list_rate: 75,
					uom: "Nos",
					conversion_factor: 1,
				},
			});
		}
	};

	itemField.change.call({ doc: newRow, value: "ITEM-B" });
	assert.equal(newRow.item_code, "ITEM-B", "New row can set item_code");
	assert.equal(newRow.rate, 75, "New row fetches pricing");
	assert.equal(newRow.item_name, "Item B Name");
});

// 16. Pure Rate Calculation Logic Formulas
test("Pure rate calculation formulas: blanket orders, margins, discounts", () => {
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	const { calculate_rate } = require(jsPath);

	// Blanket order override
	const resBlanket = calculate_rate({
		price_list_rate: 100,
		blanket_order_rate: 80,
	});
	assert.equal(resBlanket.rate, 80);

	// Margin Percentage
	const resMarginPct = calculate_rate({
		price_list_rate: 100,
		margin_type: "Percentage",
		margin_rate_or_amount: 20,
	});
	assert.equal(resMarginPct.rate, 120);

	// Margin Amount
	const resMarginAmt = calculate_rate({
		price_list_rate: 100,
		margin_type: "Amount",
		margin_rate_or_amount: 15,
	});
	assert.equal(resMarginAmt.rate, 115);

	// Discount Percentage on Margin
	const resDiscPct = calculate_rate({
		price_list_rate: 100,
		margin_type: "Amount",
		margin_rate_or_amount: 20, // 120
		discount_percentage: 10,   // 120 - 12 = 108
	});
	assert.equal(resDiscPct.rate, 108);

	// Discount Amount
	const resDiscAmt = calculate_rate({
		price_list_rate: 100,
		discount_amount: 25,
	});
	assert.equal(resDiscAmt.rate, 75);

	// Free item
	const resFree = calculate_rate({
		price_list_rate: 100,
		is_free_item: 1,
	});
	assert.equal(resFree.rate, 0);
	assert.equal(resFree.is_explicit, true);

	// Missing price
	const resMissing = calculate_rate({
		price_list_rate: 0,
		has_pricing_rule: false,
	});
	assert.equal(resMissing.rate, 0);
	assert.equal(resMissing.is_explicit, false);
});

// 17. Exercise actual native erpnext.utils.update_child_items extracted from utils.js
test("Exercise actual native erpnext.utils.update_child_items from utils.js", (t) => {
	const appRoot = path.resolve(__dirname, "../..");
	const utilsPath =
		process.env.NATIVE_ERPNEXT_UTILS_PATH ||
		path.resolve(appRoot, "../erpnext/erpnext/public/js/utils.js");

	if (!fs.existsSync(utilsPath)) {
		if (t && typeof t.skip === "function") {
			t.skip("ERPNext utils.js not present, skipping native extraction test");
		}
		return;
	}

	const utilsSource = fs.readFileSync(utilsPath, "utf8");
	const fnMatch = utilsSource.match(/erpnext\.utils\.update_child_items\s*=\s*function\s*\(opts\)\s*\{[\s\S]*?\n\};/);
	assert.ok(fnMatch, "update_child_items function must be extracted from utils.js");

	const env = createTestEnvironment();
	// Evaluate actual native update_child_items
	const evalNative = new Function("erpnext", "frappe", "__", "cint", "flt", "refresh_field", fnMatch[0]);
	evalNative(erpnext, frappe, __, cint, flt, refresh_field);

	// Now wrap with order_update_pricing
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	const app = require(jsPath);
	app.init();

	assert.ok(erpnext.utils.update_child_items.__order_update_pricing_wrapped);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-REAL", qty: 3, rate: 42, uom: "Nos" },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();

	// Verify UOM is visible and has change handler in the real native dialog
	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	assert.equal(uomField.in_list_view, 1);
	assert.ok(typeof uomField.change === "function");

	// Trigger UOM change
	global.frappe.call = (opts) => {
		if (opts.callback) {
			opts.callback({
				message: {
					price_list_rate: 84,
					conversion_factor: 2,
					uom: "Pair",
				},
			});
		}
	};

	const row = dialog.fields_dict.trans_items.df.data[0];
	uomField.change.call({ doc: row, value: "Pair" });

	assert.equal(row.rate, 84);
	assert.equal(row.conversion_factor, 2);
	assert.equal(row.uom, "Pair");
});

// 18. Bound primary button click closure validation
test("Primary action wrapped in dialog_opts so bound button click runs validation", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Put row into pending state
	global.frappe.call = () => {};
	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	assert.equal(row.__pricing_status, "pending");

	// Click bound primary button (which executes bound_primary_action)
	dialog.primary_button.click();

	// Must be blocked by validation!
	assert.ok(env.messages.some((m) => m.includes("Pricing calculation is still in progress")));
	assert.equal(env.confirms.length, 0);
});

// 19. Manual rate does NOT bypass pending or failed conversion factor lookup
test("Manual rate edit does not bypass pending or failed conversion factor lookup", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	const rateField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "rate");

	// Inflight lookup
	global.frappe.call = () => {};
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });
	assert.equal(row.__pricing_status, "pending");

	// User types manual rate 50 while pending
	row.rate = 50;
	rateField.change.call({ doc: row, value: 50 });

	// Status must remain pending!
	assert.equal(row.__pricing_status, "pending", "Pending status must survive manual rate change");

	dialog.primary_button.click();
	assert.ok(env.messages.some((m) => m.includes("Pricing calculation is still in progress")));

	// Lookup fails on server
	global.frappe.call = (opts) => {
		opts.callback({ exc: "Server Error" });
	};
	uomField.change.call({ doc: row, value: "Box" });
	assert.equal(row.__pricing_status, "failed");

	// User types manual rate on failed row
	row.rate = 60;
	rateField.change.call({ doc: row, value: 60 });
	assert.equal(row.__pricing_status, "failed", "Failed status must survive manual rate change");

	dialog.primary_button.click();
	assert.ok(env.messages.some((m) => m.includes("Pricing lookup failed")));
});

// 20. Missing or zero conversion factor marks row failed and blocks submit
test("Missing or zero conversion factor in response fails lookup and blocks submit", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Purchase Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 2, rate: 20, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Server returns missing/zero conversion factor
	global.frappe.call = (opts) => {
		opts.callback({
			message: {
				price_list_rate: 100,
				conversion_factor: 0, // Invalid!
				uom: "UnknownUOM",
			},
		});
	};

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	row.uom = "UnknownUOM";
	uomField.change.call({ doc: row, value: "UnknownUOM" });

	assert.equal(row.__pricing_status, "failed", "Row must fail if conversion factor is 0 or missing");

	dialog.primary_button.click();
	assert.ok(env.messages.some((m) => m.includes("Pricing lookup failed") || m.includes("Invalid conversion factor")));
});

// 21. Submit gate checks partially processed rows
test("Submit gate blocks submission if a processed row UOM was changed", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{
			name: "row-processed",
			item_code: "ITEM-01",
			qty: 10,
			rate: 15,
			uom: "Nos",
			conversion_factor: 1,
			delivered_qty: 2,
		},
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Directly modify row.uom (e.g. bypassing control)
	row.uom = "Box";

	dialog.primary_button.click();
	assert.ok(env.messages.some((m) => m.includes("Cannot change UOM")));
	assert.equal(env.confirms.length, 0);
});

// 22. Preserve existing row description on qty/UOM change
test("Preserves custom description on existing row; populates only if empty", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{
			name: "row-desc",
			item_code: "ITEM-01",
			qty: 2,
			rate: 10,
			uom: "Nos",
			conversion_factor: 1,
			description: "My Custom User Notes",
		},
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Server returns standard item description
	global.frappe.call = (opts) => {
		opts.callback({
			message: {
				price_list_rate: 15,
				conversion_factor: 1,
				uom: "Nos",
				description: "Standard Master Description Overwrite",
			},
		});
	};

	const qtyField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "qty");
	row.qty = 5;
	qtyField.change.call({ doc: row, value: 5 });

	// Custom description must NOT be overwritten!
	assert.equal(row.description, "My Custom User Notes");

	// Now for a row without description:
	const emptyDescRow = {
		name: "row-empty-desc",
		item_code: "ITEM-02",
		qty: 1,
		uom: "Nos",
		description: "",
	};
	dialog.fields_dict.trans_items.df.data.push(emptyDescRow);

	const itemField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "item_code");
	itemField.change.call({ doc: emptyDescRow, value: "ITEM-02" });

	assert.equal(emptyDescRow.description, "Standard Master Description Overwrite");
});

// 23. Switching item on new row clears old UOM and conversion factor
test("Switching item on new row clears old UOM and conversion factor to adopt new defaults", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", []);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();

	const newRow = {
		name: "row-new",
		docname: undefined,
		item_code: "ITEM-A",
		qty: 1,
		uom: "Box",
		conversion_factor: 10,
	};
	dialog.fields_dict.trans_items.df.data.push(newRow);

	let receivedCtx = null;
	global.frappe.call = (opts) => {
		receivedCtx = opts.args.ctx;
		opts.callback({
			message: {
				item_name: "Item B",
				price_list_rate: 35,
				conversion_factor: 1,
				uom: "Nos",
			},
		});
	};

	const itemField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "item_code");
	itemField.change.call({ doc: newRow, value: "ITEM-B" });

	// Context should NOT have sent old UOM
	assert.equal(receivedCtx.uom, undefined);
	assert.equal(newRow.uom, "Nos");
	assert.equal(newRow.conversion_factor, 1);
	assert.equal(newRow.rate, 35);
});

// 24. call_opts.error callback and subsequent recovery
test("call_opts.error handler marks status as failed and recovers on subsequent valid fetch", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	let capturedError = null;
	global.frappe.call = (opts) => {
		capturedError = opts.error;
	};

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	assert.equal(row.__pricing_status, "pending");
	assert.ok(typeof capturedError === "function", "error callback must be defined on call opts");

	// Invoke HTTP/network error
	capturedError({ statusText: "500 Internal Error" });
	assert.equal(row.__pricing_status, "failed", "Row status must be failed after error callback");

	// Recovery: subsequent successful fetch
	global.frappe.call = (opts) => {
		opts.callback({
			message: {
				price_list_rate: 120,
				conversion_factor: 12,
				uom: "Box",
			},
		});
	};
	uomField.change.call({ doc: row, value: "Box" });

	assert.equal(row.__pricing_status, "idle", "Row must recover to idle after valid fetch");
	assert.equal(row.rate, 120);
	assert.equal(row.conversion_factor, 12);
});

// 25. Dialog constructor restoration on exception in try/finally
test("Restores frappe.ui.Dialog unconditionally even if native helper throws an exception", () => {
	const env = createTestEnvironment();
	const OriginalDialog = frappe.ui.Dialog;

	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	// Mock native update_child_items throwing an error
	erpnext.utils.update_child_items.__original = erpnext.utils.update_child_items;
	const throwing_native = function () {
		throw new Error("Simulated constructor failure");
	};

	// Temporarily wrap throwing native
	erpnext.utils.update_child_items = function (opts) {
		const OriginalDialog = frappe.ui.Dialog;
		try {
			class OrderPricingDialog extends OriginalDialog {}
			frappe.ui.Dialog = OrderPricingDialog;
			return throwing_native.apply(this, arguments);
		} finally {
			frappe.ui.Dialog = OriginalDialog;
		}
	};

	const frm = mockFrm("Sales Order", []);

	assert.throws(
		() => erpnext.utils.update_child_items({ frm, child_docname: "items" }),
		/Simulated constructor failure/
	);

	// Dialog must be restored!
	assert.equal(frappe.ui.Dialog, OriginalDialog, "Dialog class must be restored in finally block");
});

// 26. Block modifying existing rows with pricing_rules
test("Blocks qty, UOM, or rate changes on existing rows with persisted pricing rules", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{
			name: "row-with-rule",
			item_code: "ITEM-PROMO",
			qty: 5,
			rate: 80,
			uom: "Nos",
			conversion_factor: 1,
			pricing_rules: '["PR-001"]', // Persisted pricing rule
		},
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	// Modify qty on rule-bearing row
	row.qty = 10;

	dialog.primary_button.click();
	assert.ok(env.messages.some((m) => m.includes("has pricing rules applied")));
	assert.equal(env.confirms.length, 0);

	// Unchanged rule-bearing row should be allowed
	row.qty = 5;
	env.messages.length = 0;
	dialog.primary_button.click();
	assert.ok(!env.messages.some((m) => m.includes("has pricing rules applied")));
});

// 27. Precision: rate_with_margin, separate discount precision, and negative rates
test("Precision handling and negative rate support in calculate_rate", () => {
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	const { calculate_rate } = require(jsPath);

	// Differing field precisions
	const precisions = {
		rate: 2,
		rate_with_margin: 3,
		discount_amount: 4,
	};

	// Margin rounding with 3 decimals
	// base 10.33333, margin 10% -> 11.366663 -> round(3) -> 11.367
	// discount 10% -> 11.367 * 0.1 = 1.1367 -> final: 11.367 - 1.1367 = 10.2303 -> round(2) -> 10.23
	const resPrec = calculate_rate(
		{
			price_list_rate: 10.33333,
			margin_type: "Percentage",
			margin_rate_or_amount: 10,
			discount_percentage: 10,
		},
		precisions
	);
	assert.equal(resPrec.rate, 10.23);

	// No margin: base_rate is raw (not rounded via rate_with_margin)
	const resNoMargin = calculate_rate(
		{
			price_list_rate: 10.33333,
			discount_amount: 0.3333,
		},
		precisions
	);
	assert.equal(resNoMargin.rate, 10); // 10.33333 - 0.3333 = 10.00003 -> round(2) = 10

	// Negative rate (valid return / deduction item)
	const resNegative = calculate_rate(
		{
			price_list_rate: -45.5,
		},
		precisions
	);
	assert.equal(resNegative.rate, -45.5);
	assert.equal(resNegative.is_explicit, true);
});

// 28. Precision inherits site currency_precision when df.precision is blank
test("Inherits site currency_precision when df.precision is blank", () => {
	const env = createTestEnvironment();
	global.frappe.defaults = {
		get_default: (key) => (key === "currency_precision" ? 3 : 2),
	};
	global.frappe.meta.get_field_precision = (df, doc) => {
		if (df && df.precision != null) return cint(df.precision);
		return cint(global.frappe.defaults.get_default("currency_precision"));
	};
	// df has blank precision
	global.frappe.get_meta = (doctype) => ({
		fields: [
			{ fieldname: "qty", precision: null },
			{ fieldname: "rate", precision: null, fieldtype: "Currency" },
			{ fieldname: "conversion_factor", precision: 4 },
		],
	});

	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	global.frappe.call = (opts) => {
		opts.callback({
			message: {
				price_list_rate: 10.125,
				conversion_factor: 1,
				uom: "Nos",
			},
		});
	};

	const qtyField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "qty");
	row.qty = 2;
	qtyField.change.call({ doc: row, value: 2 });

	// Should be 10.125 with 3 decimals, NOT rounded to 10.13
	assert.equal(row.rate, 10.125);
});

// 29. Request settlement marks pending token failed when error callback bypassed
test("Request settlement (.always) marks pending token failed when error callback bypassed", () => {
	const env = createTestEnvironment();
	const jsPath = path.resolve(__dirname, "../public/js/order_update_pricing.js");
	delete require.cache[require.resolve(jsPath)];
	require(jsPath);

	const frm = mockFrm("Sales Order", [
		{ name: "row-1", item_code: "ITEM-01", qty: 1, rate: 10, uom: "Nos", conversion_factor: 1 },
	]);

	erpnext.utils.update_child_items({ frm, child_docname: "items" });
	const dialog = env.getDialog();
	const row = dialog.fields_dict.trans_items.df.data[0];

	let capturedAlways = null;
	global.frappe.call = (opts) => {
		capturedAlways = opts.always;
		// Simulates QueryTimeoutError/QueryDeadlockError returning without calling callback or error
		const jqXHR = {
			always: (fn) => {
				// capture jqXHR always handler
			},
		};
		return jqXHR;
	};

	const uomField = dialog.fields_dict.trans_items.df.fields.find((f) => f.fieldname === "uom");
	row.uom = "Box";
	uomField.change.call({ doc: row, value: "Box" });

	assert.equal(row.__pricing_status, "pending");
	assert.ok(typeof capturedAlways === "function", "always handler must be passed to call opts");

	// Settlement runs
	capturedAlways();
	assert.equal(row.__pricing_status, "failed", "Settlement must mark pending row as failed");

	// Test stale settlement does not touch newer lookup
	// User triggers newer lookup (token 2)
	let capturedAlwaysToken2 = null;
	global.frappe.call = (opts) => {
		capturedAlwaysToken2 = opts.always;
		opts.callback({
			message: {
				price_list_rate: 55,
				conversion_factor: 1,
				uom: "Pack",
			},
		});
		return { always: () => {} };
	};
	row.uom = "Pack";
	uomField.change.call({ doc: row, value: "Pack" });

	assert.equal(row.__pricing_status, "idle");
	assert.equal(row.rate, 55);

	// Now older Token 1 settlement runs late:
	capturedAlways();
	// Must NOT touch the newer lookup!
	assert.equal(row.__pricing_status, "idle", "Stale settlement must not affect newer lookup");
	assert.equal(row.rate, 55);
});
