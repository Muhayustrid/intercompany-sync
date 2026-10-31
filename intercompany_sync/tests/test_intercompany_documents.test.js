import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const JS_PATH = path.resolve("intercompany_sync/public/js/intercompany_documents.js");

/**
 * Build a realistic Frappe browser environment stubbed for testing.
 */
function createFrappeEnvironment() {
	const handlers = {};
	const calls = [];
	const syncedDocs = [];
	const messages = [];

	class PageStub {
		constructor() {
			this.inner_buttons = {};
		}
		add_inner_button(label, action, group) {
			const grp = group || "";
			this.inner_buttons[grp] = this.inner_buttons[grp] || [];
			if (this.inner_buttons[grp].some((b) => b.label === label)) {
				// True Frappe behavior: silently ignore duplicate label in same group
				return;
			}
			this.inner_buttons[grp].push({ label, action });
		}
		remove_inner_button(label, group) {
			const grp = group || "";
			if (!this.inner_buttons[grp]) return;
			const targets = Array.isArray(label) ? label : [label];
			this.inner_buttons[grp] = this.inner_buttons[grp].filter(
				(b) => !targets.includes(b.label)
			);
		}
	}

	class DialogStub {
		constructor(opts) {
			this.opts = opts;
			this.title = opts.title;
			this.fields = opts.fields || [];
			this.fields_dict = {};
			this.values = {};
			this.is_shown = false;
			this.status_html = "";

			for (const f of this.fields) {
				this.fields_dict[f.fieldname] = {
					df: { ...f },
					set_data: (options) => {
						this.fields_dict[f.fieldname].df.options = options;
					},
					html: (content) => {
						this.status_html = content;
					},
					$wrapper: {
						html: (c) => {
							this.status_html = c;
						},
						empty: () => {},
					},
				};
			}
		}
		show() {
			this.is_shown = true;
		}
		hide() {
			this.is_shown = false;
		}
		set_df_property(fieldname, prop, val) {
			if (this.fields_dict[fieldname]) {
				this.fields_dict[fieldname].df[prop] = val;
				if (fieldname === "status_html" && prop === "options") {
					this.status_html = val;
				}
			}
		}
		set_value(fieldname, val) {
			this.values[fieldname] = val;
			const field = this.fields_dict[fieldname];
			if (field && field.df && typeof field.df.onchange === "function") {
				field.df.onchange();
			}
		}
		get_value(fieldname) {
			return this.values[fieldname];
		}
		submit_primary() {
			if (typeof this.opts.primary_action === "function") {
				this.opts.primary_action(this.values);
			}
		}
	}

	class FormStub {
		constructor(doctype, doc = {}) {
			this.doctype = doctype;
			this.doc = {
				doctype,
				name: doc.name || `new-${doctype.toLowerCase().replace(/ /g, "-")}-1`,
				__islocal: 1,
				docstatus: 0,
				company: "Company A",
				items: [],
				...doc,
			};
			this.page = new PageStub();
			this.custom_buttons = {};
			this.is_dirty = false;
			this.refresh_count = 0;
			this.saved = false;
			this.submitted = false;
		}
		add_custom_button(label, action, group) {
			this.custom_buttons[label] = action;
			return this.page.add_inner_button(label, action, group);
		}
		remove_custom_button(label, group) {
			this.page.remove_inner_button(label, group);
			delete this.custom_buttons[label];
		}
		dirty() {
			this.is_dirty = true;
		}
		refresh() {
			this.refresh_count += 1;
		}
		save() {
			this.saved = true;
		}
		savesubmit() {
			this.submitted = true;
		}
	}

	const permissions = {
		"Purchase Order": { read: true, create: true },
		"Sales Order": { read: true, create: true },
		"Delivery Note": { read: true, create: true },
		"Purchase Receipt": { read: true, create: true },
	};

	let lastDialog = null;
	const routes = [];

	const frappe = {
		__: (str) => str,
		provide: (ns) => {},
		route_options: null,
		set_route: (...args) => {
			routes.push(args);
		},
		ui: {
			form: {
				on: (doctype, event_handlers) => {
					handlers[doctype] = handlers[doctype] || [];
					handlers[doctype].push(event_handlers);
				},
				MultiSelectDialog: function (opts) {
					const picker = {
						opts,
						is_shown: true,
						select: (names) => opts.action(names, {}),
					};
					picker.dialog = { hide: () => (picker.is_shown = false) };
					lastDialog = picker;
					return picker;
				},
			},
			Dialog: function (opts) {
				const d = new DialogStub(opts);
				lastDialog = d;
				return d;
			},
		},
		model: {
			can_read: (dt) => permissions[dt]?.read ?? true,
			can_create: (dt) => permissions[dt]?.create ?? true,
			sync: (doc) => {
				syncedDocs.push(doc);
			},
			open_mapped_doc: (opts) => {
				calls.push({ type: "open_mapped_doc", ...opts });
			},
		},
		msgprint: (msg) => {
			messages.push(msg);
		},
		throw: (msg) => {
			messages.push(msg);
			throw new Error(typeof msg === "string" ? msg : msg.message);
		},
		call: (opts) => {
			calls.push({ type: "call", ...opts });
			// Default immediate callback if provided
			if (typeof opts.callback === "function" && opts.__auto_respond) {
				opts.callback(opts.__auto_respond);
			}
		},
	};

	const __ = (str) => str;
	const ctx = {
		console,
		frappe,
		__,
		FormStub,
		handlers,
		calls,
		syncedDocs,
		messages,
		permissions,
		routes,
		getLastDialog: () => lastDialog,
	};
	ctx.window = ctx;
	ctx.globalThis = ctx;
	return ctx;
}

function loadScriptInVm(context) {
	const code = fs.readFileSync(JS_PATH, "utf8");
	vm.runInNewContext(code, context);
}

function triggerEvent(context, doctype, eventName, frm) {
	const dtHandlers = context.handlers[doctype] || [];
	for (const h of dtHandlers) {
		if (typeof h[eventName] === "function") {
			h[eventName](frm);
		}
	}
}

test("Idempotent registration: repeated script loads do not register duplicate handlers", () => {
	const ctx = createFrappeEnvironment();
	// Simulate loading shared JS 4 times (for SO, PO, DN, PR)
	loadScriptInVm(ctx);
	loadScriptInVm(ctx);
	loadScriptInVm(ctx);
	loadScriptInVm(ctx);

	assert.equal(ctx.handlers["Sales Order"]?.length, 1, "Sales Order handler registered exactly once");
	assert.equal(ctx.handlers["Purchase Order"]?.length, 1, "Purchase Order handler registered exactly once");
	assert.equal(ctx.handlers["Delivery Note"]?.length, 1, "Delivery Note handler registered exactly once");
	assert.equal(ctx.handlers["Purchase Receipt"]?.length, 1, "Purchase Receipt handler registered exactly once");
});

test("Sales Order: draft with PO read permission adds 'Inter Company Purchase Order' button under 'Get Items From'", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	const buttons = frm.page.inner_buttons["Get Items From"] || [];
	const btn = buttons.find((b) => b.label === "Inter Company Purchase Order");
	assert.ok(btn, "Button 'Inter Company Purchase Order' should exist under 'Get Items From'");

	// Multiple refreshes do not duplicate button
	triggerEvent(ctx, "Sales Order", "refresh", frm);
	const buttonsAfter = frm.page.inner_buttons["Get Items From"] || [];
	const btnCount = buttonsAfter.filter((b) => b.label === "Inter Company Purchase Order").length;
	assert.equal(btnCount, 1, "Should not duplicate button on multiple refreshes");
});

test("Sales Order: no button if submitted or lacks PO read permission", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	// Submitted
	const frmSubmitted = new ctx.FormStub("Sales Order", { docstatus: 1 });
	triggerEvent(ctx, "Sales Order", "refresh", frmSubmitted);
	assert.equal(frmSubmitted.page.inner_buttons["Get Items From"], undefined);

	// No permission
	ctx.permissions["Purchase Order"].read = false;
	const frmDraft = new ctx.FormStub("Sales Order", { docstatus: 0 });
	triggerEvent(ctx, "Sales Order", "refresh", frmDraft);
	assert.equal(frmDraft.page.inner_buttons["Get Items From"], undefined);
});

test("Purchase Receipt: draft with DN read permission adds 'Inter Company Delivery Note' button", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Receipt", {
		docstatus: 0,
		company: "Company B",
		supplier: "Supp-A",
	});

	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);
	const buttons = frm.page.inner_buttons["Get Items From"] || [];
	const btn = buttons.find((b) => b.label === "Inter Company Delivery Note");
	assert.ok(btn, "Button 'Inter Company Delivery Note' should exist under 'Get Items From'");

	// Multiple refreshes do not duplicate button
	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);
	const btnCount = (frm.page.inner_buttons["Get Items From"] || []).filter(
		(b) => b.label === "Inter Company Delivery Note"
	).length;
	assert.equal(btnCount, 1, "Should not duplicate button on multiple refreshes");
});

test("Purchase Receipt: no button if submitted or lacks DN read permission", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	// Submitted
	const frmSubmitted = new ctx.FormStub("Purchase Receipt", { docstatus: 1 });
	triggerEvent(ctx, "Purchase Receipt", "refresh", frmSubmitted);
	assert.equal(frmSubmitted.page.inner_buttons["Get Items From"], undefined);

	// No permission
	ctx.permissions["Delivery Note"].read = false;
	const frmDraft = new ctx.FormStub("Purchase Receipt", { docstatus: 0 });
	triggerEvent(ctx, "Purchase Receipt", "refresh", frmDraft);
	assert.equal(frmDraft.page.inner_buttons["Get Items From"], undefined);
});

test("Delivery Note: submitted intercompany replaces native button with partial-capable wrapper", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Delivery Note", {
		docstatus: 1,
		is_internal_customer: 1,
		company: "Company A",
		represents_company: "Company B",
	});

	// Simulate native delivery_note.js adding old button first
	frm.add_custom_button(
		"Inter Company Purchase Receipt",
		() => {
			ctx.calls.push({ type: "native_button_clicked" });
		},
		"Create"
	);
	assert.equal(frm.page.inner_buttons["Create"]?.length, 1);

	// Run our refresh handler: should remove native button and replace with wrapper
	triggerEvent(ctx, "Delivery Note", "refresh", frm);

	const buttons = frm.page.inner_buttons["Create"] || [];
	assert.equal(buttons.length, 1, "Should still have exactly one button under Create");
	const btn = buttons[0];
	assert.equal(btn.label, "Inter Company Purchase Receipt");

	// Click button: must call frappe.model.open_mapped_doc with intercompany_sync mapping method
	btn.action();
	const call = ctx.calls.find((c) => c.type === "open_mapped_doc");
	assert.ok(call, "frappe.model.open_mapped_doc should be called");
	assert.equal(call.method, "intercompany_sync.mapping.make_purchase_receipt");
	assert.equal(call.frm, frm);
});

test("Delivery Note: adds button even when inter_company_reference is set (partial receipt support)", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	// In native ERPNext, if inter_company_reference is set, button is skipped.
	// Our handler must still provide the button to allow mapping remaining outstanding.
	const frm = new ctx.FormStub("Delivery Note", {
		docstatus: 1,
		is_internal_customer: 1,
		company: "Company A",
		represents_company: "Company B",
		inter_company_reference: "MAT-PR-2026-00001",
	});

	triggerEvent(ctx, "Delivery Note", "refresh", frm);
	const buttons = frm.page.inner_buttons["Create"] || [];
	const btn = buttons.find((b) => b.label === "Inter Company Purchase Receipt");
	assert.ok(btn, "Button must be added even when inter_company_reference exists");
});

test("Delivery Note: no button if draft, not internal customer, or cannot create PR", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	// Draft
	const frmDraft = new ctx.FormStub("Delivery Note", { docstatus: 0, is_internal_customer: 1 });
	triggerEvent(ctx, "Delivery Note", "refresh", frmDraft);
	assert.equal(frmDraft.page.inner_buttons["Create"], undefined);

	// Not internal
	const frmExternal = new ctx.FormStub("Delivery Note", { docstatus: 1, is_internal_customer: 0 });
	triggerEvent(ctx, "Delivery Note", "refresh", frmExternal);
	assert.equal(frmExternal.page.inner_buttons["Create"], undefined);

	// Cannot create PR
	ctx.permissions["Purchase Receipt"].create = false;
	const frmNoPerm = new ctx.FormStub("Delivery Note", { docstatus: 1, is_internal_customer: 1 });
	triggerEvent(ctx, "Delivery Note", "refresh", frmNoPerm);
	assert.equal(frmNoPerm.page.inner_buttons["Create"], undefined);
});

test("Picker dialog: native MultiSelectDialog queries search_sources with target company and party", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	const btn = frm.page.inner_buttons["Get Items From"].find(
		(b) => b.label === "Inter Company Purchase Order"
	);
	btn.action();

	const picker = ctx.getLastDialog();
	assert.ok(picker, "MultiSelectDialog should be created");
	assert.equal(picker.opts.doctype, "Purchase Order");
	assert.equal(picker.opts.add_filters_group, 1);
	assert.deepEqual(
		Array.from(picker.opts.setters, (s) => s.fieldname),
		["company", "transaction_date"]
	);
	const query = picker.opts.get_query();
	assert.equal(query.query, "intercompany_sync.mapping.search_sources");
	assert.equal(query.filters.target_company, "Company B");
	assert.equal(query.filters.target_party, "Cust-A");
});

test("Picker dialog: Purchase Receipt uses Delivery Note source with posting_date setter and supplier party", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Receipt", {
		docstatus: 0,
		company: "Company B",
		supplier: "Supp-A",
	});

	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const picker = ctx.getLastDialog();

	assert.equal(picker.opts.doctype, "Delivery Note");
	assert.deepEqual(
		Array.from(picker.opts.setters, (s) => s.fieldname),
		["company", "posting_date"]
	);
	assert.equal(picker.opts.get_query().filters.target_party, "Supp-A");
});

test("Picker dialog: requires exactly one selected source and never maps several", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Receipt", {
		docstatus: 0,
		company: "Company B",
		supplier: "Supp-A",
	});

	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const picker = ctx.getLastDialog();

	picker.select([]);
	picker.select(["DN-001", "DN-002"]);
	assert.equal(
		ctx.calls.filter((c) => c.method === "intercompany_sync.mapping.make_purchase_receipt").length,
		0
	);
	assert.equal(ctx.messages.length, 2);

	picker.select(["DN-001"]);
	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_purchase_receipt");
	assert.equal(mapCall.args.source_name, "DN-001");
});

test("Get Items replaces a filled item table like native, without touching the form before the response", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		items: [{ doctype: "Sales Order Item", name: "row-1", item_code: "ROTI-001", qty: 5 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	ctx.getLastDialog().select(["PUR-ORD-2026-00001"]);

	assert.equal(ctx.messages.length, 0, "A filled table must not be rejected");
	assert.equal(frm.doc.items[0].item_code, "ROTI-001", "Form untouched until the server responds");

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	const targetPayload = JSON.parse(mapCall.args.target_doc);
	assert.equal(targetPayload.name, "new-sales-order-1");
	assert.equal(targetPayload.customer, "Cust-A");
	assert.equal(targetPayload.items.length, 0, "Existing rows are dropped so the mapped rows replace them");
});

test("Async stale guard: aborts applying mapped document if target company/party/name changed", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		items: [],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	dialog.select(["PUR-ORD-2026-00001"]);

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall);

	// User changed target customer while RPC was in flight
	frm.doc.customer = "Cust-DIFFERENT";

	// Server returns mapped doc
	mapCall.callback({
		message: {
			doctype: "Sales Order",
			name: "new-sales-order-1",
			customer: "Cust-A",
			items: [{ item_code: "ROTI-001", qty: 10 }],
		},
	});

	// Must NOT sync to locals or dirty form
	assert.equal(ctx.syncedDocs.length, 0, "Must NOT sync mapped result when target party changed");
	assert.equal(frm.is_dirty, false);
});

test("Mapping creates draft only: syncs locals, marks dirty and refreshed, never saves or submits", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		items: [],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	dialog.select(["PUR-ORD-2026-00001"]);

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	const mappedDoc = {
		doctype: "Sales Order",
		name: "new-sales-order-1",
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		items: [{ item_code: "ROTI-001", qty: 10 }],
	};

	mapCall.callback({ message: mappedDoc });

	assert.equal(ctx.syncedDocs.length, 1);
	assert.equal(ctx.syncedDocs[0], mappedDoc);
	assert.equal(frm.is_dirty, true, "Form must be marked dirty");
	assert.ok(frm.refresh_count > 0, "Form must be refreshed");
	assert.equal(frm.saved, false, "Must NOT automatically save form");
	assert.equal(frm.submitted, false, "Must NOT automatically submit form");
	assert.equal(dialog.is_shown, false, "Dialog must be closed on success");
});

test("Async stale guard: aborts applying mapped document if header fields (date/warehouse) changed during RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		transaction_date: "2026-10-30",
		items: [],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	dialog.select(["PUR-ORD-2026-00001"]);

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall);

	// Programmatic modification to header date while RPC was in flight
	frm.doc.transaction_date = "2026-11-01";

	mapCall.callback({
		message: {
			doctype: "Sales Order",
			name: "new-sales-order-1",
			docstatus: 0,
			company: "Company B",
			customer: "Cust-A",
			transaction_date: "2026-10-30",
			items: [{ item_code: "ROTI-001", qty: 10 }],
		},
	});

	assert.equal(ctx.syncedDocs.length, 0, "Must NOT sync mapped result when header date changed");
	assert.equal(frm.is_dirty, false);
	assert.equal(frm.doc.transaction_date, "2026-11-01", "Programmatic date edit must be preserved");
});

test("Async stale guard: aborts applying mapped document if items child table changed during RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		items: [],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	dialog.select(["PUR-ORD-2026-00001"]);

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall);

	// Programmatically add an item to the target while RPC was in flight
	frm.doc.items.push({ item_code: "ROTI-999", qty: 2 });

	mapCall.callback({
		message: {
			doctype: "Sales Order",
			name: "new-sales-order-1",
			docstatus: 0,
			company: "Company B",
			customer: "Cust-A",
			items: [{ item_code: "ROTI-001", qty: 10 }],
		},
	});

	assert.equal(ctx.syncedDocs.length, 0, "Must NOT sync mapped result when target items changed");
	assert.equal(frm.is_dirty, false);
	assert.equal(frm.doc.items.length, 1, "Delayed item addition must be preserved");
	assert.equal(frm.doc.items[0].item_code, "ROTI-999");
});

test("Delivery Note: rejects return DN (is_return=1) and removes native button without removing Sales Return", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Delivery Note", {
		docstatus: 1,
		is_internal_customer: 1,
		is_return: 1,
		company: "Company A",
		represents_company: "Company B",
	});

	// Native adds return button and native IC PR button
	frm.add_custom_button("Sales Return", () => {}, "Create");
	frm.add_custom_button("Inter Company Purchase Receipt", () => {}, "Create");

	triggerEvent(ctx, "Delivery Note", "refresh", frm);

	const buttons = frm.page.inner_buttons["Create"] || [];
	const icBtn = buttons.find((b) => b.label === "Inter Company Purchase Receipt");
	const returnBtn = buttons.find((b) => b.label === "Sales Return");

	assert.equal(icBtn, undefined, "Inter Company Purchase Receipt button must NOT exist on return DN");
	assert.ok(returnBtn, "Legitimate Sales Return button must remain untouched");
});

test("Delivery Note: rejects closed DN (status='Closed') and removes native button", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Delivery Note", {
		docstatus: 1,
		is_internal_customer: 1,
		status: "Closed",
		company: "Company A",
		represents_company: "Company B",
	});

	// Native might have added the button
	frm.add_custom_button("Inter Company Purchase Receipt", () => {}, "Create");

	triggerEvent(ctx, "Delivery Note", "refresh", frm);

	const buttons = frm.page.inner_buttons["Create"] || [];
	const icBtn = buttons.find((b) => b.label === "Inter Company Purchase Receipt");
	assert.equal(icBtn, undefined, "Inter Company Purchase Receipt button must NOT exist on closed DN");
});

test("Dashboard adapter: splits comma-separated data-names into array for list route_options", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
	});

	let originalCalled = false;
	frm.dashboard = {
		internal_links_found: [
			{ doctype: "Delivery Note", names: ["DN-001", "DN-002"], count: 2, open_count: 0 },
		],
		open_document_list: function () {
			originalCalled = true;
		},
	};

	triggerEvent(ctx, "Sales Order", "refresh", frm);

	assert.equal(frm.dashboard._ic_open_adapted, true, "Dashboard open_document_list must be adapted");

	const fakeLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Delivery Note";
			if (name === "data-names") return "DN-001,DN-002";
			return null;
		},
	};

	const res = frm.dashboard.open_document_list(fakeLink, false);
	assert.equal(originalCalled, false, "Original open_document_list must NOT be called for internal link");
	assert.equal(ctx.frappe.route_options.name[0], "in");
	assert.deepEqual(
		[...ctx.frappe.route_options.name[1]],
		["DN-001", "DN-002"],
		"data-names string must be split into Array for exact in filter"
	);
	assert.deepEqual(
		ctx.routes.map((r) => [...r]),
		[["List", "Delivery Note", "List"]]
	);
});

test("Dashboard adapter: zero or empty names returns false without opening unrestricted list", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Order", {
		docstatus: 1,
		company: "Company A",
		supplier: "Supp-B",
	});

	let originalCalled = false;
	frm.dashboard = {
		internal_links_found: [
			{ doctype: "Purchase Receipt", names: [], count: 0, open_count: 0 },
		],
		open_document_list: function () {
			originalCalled = true;
		},
	};

	triggerEvent(ctx, "Purchase Order", "refresh", frm);

	const fakeLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Purchase Receipt";
			if (name === "data-names") return "";
			return null;
		},
	};

	const res = frm.dashboard.open_document_list(fakeLink, false);
	assert.equal(res, false, "Must return false on zero names");
	assert.equal(ctx.routes.length, 0, "Must never navigate to unrestricted list on zero names");
	assert.equal(originalCalled, false);
});

test("Dashboard adapter: unrelated link delegates to original open_document_list", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Delivery Note", {
		docstatus: 1,
		company: "Company A",
		customer: "Cust-B",
	});

	let originalCalled = false;
	frm.dashboard = {
		internal_links_found: [
			{ doctype: "Purchase Receipt", names: ["PR-001"], count: 1, open_count: 0 },
		],
		open_document_list: function ($link, show_open) {
			originalCalled = true;
			return "delegated";
		},
	};

	triggerEvent(ctx, "Delivery Note", "refresh", frm);

	const fakeLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Sales Invoice";
			return null;
		},
	};

	const res = frm.dashboard.open_document_list(fakeLink, true);
	assert.equal(originalCalled, true, "Unrelated doctype must delegate to original open_document_list");
	assert.equal(res, "delegated");
});

test("Dashboard adapter: self-type link (e.g. Purchase Receipt Returns on PR) delegates to original open_document_list", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Receipt", {
		docstatus: 1,
		company: "Company A",
		supplier: "Supp-B",
	});

	let originalCalled = false;
	frm.dashboard = {
		internal_links_found: [
			{ doctype: "Delivery Note", names: ["DN-001"], count: 1, open_count: 0 },
			{ doctype: "Purchase Receipt", names: ["PR-RETURN-001"], count: 1, open_count: 0 },
		],
		open_document_list: function () {
			originalCalled = true;
			return "native_self_returns";
		},
	};

	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);

	const fakeLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Purchase Receipt"; // Same type as frm.doctype
			if (name === "data-names") return "PR-RETURN-001";
			return null;
		},
	};

	const res = frm.dashboard.open_document_list(fakeLink, false);
	assert.equal(originalCalled, true, "Self-type Returns link must delegate to native open_document_list");
	assert.equal(res, "native_self_returns");
});

test("Dashboard adapter: show_open true routes exact open_names and blocks when open_names empty", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 1,
		company: "Company B",
		customer: "Cust-A",
	});

	frm.dashboard = {
		internal_links_found: [
			{
				doctype: "Delivery Note",
				names: ["DN-DRAFT", "DN-SUBMITTED"],
				open_names: ["DN-DRAFT"],
				count: 2,
				open_count: 1,
			},
			{
				doctype: "Purchase Receipt",
				names: ["PR-SUBMITTED"],
				open_names: [],
				count: 1,
				open_count: 0,
			},
		],
		open_document_list: function () {},
	};

	triggerEvent(ctx, "Sales Order", "refresh", frm);

	// Case 1: show_open=true on Delivery Note with 1 open doc -> routes ["DN-DRAFT"]
	const dnLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Delivery Note";
			if (name === "data-names") return "DN-DRAFT,DN-SUBMITTED";
			return null;
		},
	};

	frm.dashboard.open_document_list(dnLink, true);
	assert.equal(ctx.frappe.route_options.name[0], "in");
	assert.deepEqual(
		[...ctx.frappe.route_options.name[1]],
		["DN-DRAFT"],
		"show_open=true must route open_names only"
	);

	// Case 2: show_open=false on Delivery Note -> routes all names
	frm.dashboard.open_document_list(dnLink, false);
	assert.deepEqual(
		[...ctx.frappe.route_options.name[1]],
		["DN-DRAFT", "DN-SUBMITTED"],
		"show_open=false must route all names"
	);

	// Case 3: show_open=true on Purchase Receipt with 0 open docs -> returns false, does not navigate
	ctx.routes.length = 0;
	const prLink = {
		attr: (name) => {
			if (name === "data-doctype") return "Purchase Receipt";
			if (name === "data-names") return "PR-SUBMITTED";
			return null;
		},
	};

	const resPr = frm.dashboard.open_document_list(prLink, true);
	assert.equal(resPr, false, "show_open on 0 open docs must return false");
	assert.equal(ctx.routes.length, 0, "show_open on 0 open docs must not navigate");
});
