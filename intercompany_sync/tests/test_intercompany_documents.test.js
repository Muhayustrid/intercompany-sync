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

test("Picker dialog: queries get_sources with target filters and text search", () => {
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

	const dialog = ctx.getLastDialog();
	assert.ok(dialog, "Dialog should be created and shown");
	assert.ok(dialog.is_shown);

	// Check get_sources RPC call
	const call = ctx.calls.find(
		(c) => c.method === "intercompany_sync.mapping.get_sources"
	);
	assert.ok(call, "get_sources RPC should be dispatched");
	assert.equal(call.args.source_doctype, "Purchase Order");
	assert.equal(call.args.target_company, "Company B");
	assert.equal(call.args.target_party, "Cust-A");
	assert.equal(call.args.txt, "");

	// Simulate text search in dialog
	dialog.set_value("search_text", "PO-2026");
	const searchCall = ctx.calls.filter(
		(c) => c.method === "intercompany_sync.mapping.get_sources"
	).pop();
	assert.equal(searchCall.args.txt, "PO-2026");
});

test("Picker dialog: handles empty and error states with retry, and async stale guard", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Purchase Receipt", {
		docstatus: 0,
		company: "Company B",
		supplier: "Supp-A",
	});

	triggerEvent(ctx, "Purchase Receipt", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	// 1. Simulate empty result
	const fetchCall1 = ctx.calls.filter((c) => c.method === "intercompany_sync.mapping.get_sources")[0];
	fetchCall1.callback({ message: [] });
	assert.match(dialog.status_html, /tidak ada/i, "Empty state should show clear message");

	// 2. Simulate error result
	fetchCall1.callback({ exc: "Error connecting" });
	assert.match(dialog.status_html, /gagal/i, "Error state should show failure message");
	assert.ok(dialog.retry_action, "Retry callback should be available");

	// 3. Async stale guard: earlier request completing after later request must not overwrite
	let cb1, cb2;
	ctx.calls.length = 0;
	dialog.set_value("search_text", "first");
	cb1 = ctx.calls[ctx.calls.length - 1].callback;

	dialog.set_value("search_text", "second");
	cb2 = ctx.calls[ctx.calls.length - 1].callback;

	// Request 2 completes first
	cb2({
		message: [{ name: "DN-002", company: "Company A" }],
	});
	assert.equal(dialog.fields_dict.source_name.df.options.some((o) => o.value === "DN-002"), true);

	// Request 1 completes later (stale)
	cb1({
		message: [{ name: "DN-001", company: "Company A" }],
	});
	// DN-001 must not overwrite DN-002
	assert.equal(dialog.fields_dict.source_name.df.options.some((o) => o.value === "DN-001"), false);
});

test("Target document safety: placeholder pruned in cloned payload, original draft preserved", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		// Single blank placeholder row in fresh form
		items: [{ doctype: "Sales Order Item", name: "row-1", item_code: "", qty: 0 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	// Select source and submit
	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

	// Verify original frm.doc was NOT mutated before success
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].name, "row-1");

	// Check mapping RPC call
	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall, "Mapping RPC make_sales_order must be called");
	assert.equal(mapCall.args.source_name, "PUR-ORD-2026-00001");

	const targetPayload = JSON.parse(mapCall.args.target_doc);
	assert.equal(targetPayload.name, "new-sales-order-1");
	assert.equal(targetPayload.__islocal, 1);
	assert.equal(targetPayload.company, "Company B");
	assert.equal(targetPayload.customer, "Cust-A");
	// Blank placeholder was pruned in the cloned payload
	assert.equal(targetPayload.items.length, 0);
});

test("Target document safety: occupied target rejected without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// Occupied target: contains a real item
		items: [{ doctype: "Sales Order Item", item_code: "ROTI-001", qty: 5 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when target is occupied");
	assert.equal(frm.doc.items.length, 1, "Original items must remain untouched");
	assert.equal(frm.doc.items[0].item_code, "ROTI-001");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
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

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

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

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

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

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

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

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

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

test("Target safety: rejects target with non-default qty in single row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User typed qty: 5 on the row before setting item_code
		items: [{ doctype: "Sales Order Item", item_code: "", qty: 5 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not even open when target has user qty edit");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has user qty edit");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].qty, 5, "User qty edit must be retained untouched");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: rejects target with warehouse set in single row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User selected warehouse on the row before setting item_code
		items: [{ doctype: "Sales Order Item", item_code: "", warehouse: "Stores - B" }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not even open when target has user warehouse edit");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has warehouse edit");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].warehouse, "Stores - B", "User warehouse must be retained untouched");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: accepts virgin placeholder with default blank native qty 1 and no user edits", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		// Default virgin placeholder with qty: 1 and no warehouse or user edits
		items: [{ doctype: "Sales Order Item", name: "row-1", item_code: "", qty: 1 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall, "Virgin placeholder with qty 1 should be accepted and pruned");
	const targetPayload = JSON.parse(mapCall.args.target_doc);
	assert.equal(targetPayload.items.length, 0, "Virgin placeholder pruned in payload");
	assert.equal(frm.doc.items.length, 1, "Original doc untouched before RPC response");
});

test("Target safety: rejects single row with qty 1 when __unedited is false (user edited row)", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// Row where user actively set a field, unsetting __unedited
		items: [{ doctype: "Sales Order Item", item_code: "", qty: 1, __unedited: false }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not even open when row has __unedited=false");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must reject row that user explicitly edited");
	assert.equal(frm.doc.items.length, 1);
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

test("Target safety: rejects target with batch_no set on row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User selected or entered batch_no on a row before setting item_code
		items: [{ doctype: "Sales Order Item", item_code: "", batch_no: "BATCH-2026-001" }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not open when target row has batch_no");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has batch_no");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].batch_no, "BATCH-2026-001", "User batch_no must be preserved");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: rejects target with delivery_date or schedule_date set on row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User set schedule/delivery date on a row before setting item_code
		items: [{ doctype: "Sales Order Item", item_code: "", delivery_date: "2026-11-05" }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not open when target row has delivery_date");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has delivery_date");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].delivery_date, "2026-11-05", "User delivery_date must be preserved");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: rejects target with negative rate set on row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User typed negative rate (e.g. discount or credit row)
		items: [{ doctype: "Sales Order Item", item_code: "", rate: -15.5 }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not open when target row has negative rate");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has negative rate");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].rate, -15.5, "Negative rate must be preserved");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: rejects target with custom meaningful string field set on row without item_code without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User entered custom note or custom field on a row before setting item_code
		items: [{ doctype: "Sales Order Item", item_code: "", custom_po_reference: "PO-CLIENT-99" }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not open when target row has custom meaningful string");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has custom field");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].custom_po_reference, "PO-CLIENT-99", "Custom field value must be preserved");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
});

test("Target safety: accepts untouched native default row with zero flags and allowed 1 defaults (qty, conversion_factor, reserve_stock)", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		// Native schema defaults on fresh untouched child row
		items: [
			{
				doctype: "Sales Order Item",
				name: "row-1",
				docstatus: 0,
				__islocal: 1,
				__unsaved: 1,
				idx: 1,
				item_code: "",
				description: "",
				qty: 1,
				conversion_factor: 1,
				reserve_stock: 1,
				is_free_item: 0,
				delivered_by_supplier: 0,
				page_break: 0,
				against_blanket_order: 0,
				grant_commission: 0,
				stock_reserved_qty: 0,
				is_stock_item: 0,
				rate: 0,
				amount: 0,
				stock_qty: 0,
			},
		],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();
	assert.ok(dialog, "Dialog must open when child row has only native schema defaults");

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall, "Mapping RPC must be dispatched for untouched native defaults");
	const targetPayload = JSON.parse(mapCall.args.target_doc);
	assert.equal(targetPayload.items.length, 0, "Virgin row with native defaults pruned in payload");
	assert.equal(frm.doc.items.length, 1, "Original doc untouched before RPC response");
});

test("Target safety: accepts untouched native default row with string defaults qty:'1', conversion_factor:'1', and zero flag:'0'", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		name: "new-sales-order-1",
		company: "Company B",
		customer: "Cust-A",
		// Untouched native row where schema defaults are represented as strings (common from DocType schema defaults)
		items: [
			{
				doctype: "Sales Order Item",
				name: "row-1",
				docstatus: 0,
				__islocal: 1,
				__unsaved: 1,
				idx: 1,
				item_code: "",
				qty: "1",
				conversion_factor: "1",
				reserve_stock: "1",
				is_free_item: "0",
				delivered_by_supplier: "0",
				rate: "0",
			},
		],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();
	const dialog = ctx.getLastDialog();
	assert.ok(dialog, "Dialog must open when child row has schema default strings '1' and '0'");

	dialog.set_value("source_name", "PUR-ORD-2026-00001");
	dialog.submit_primary();

	const mapCall = ctx.calls.find((c) => c.method === "intercompany_sync.mapping.make_sales_order");
	assert.ok(mapCall, "Mapping RPC must be dispatched for untouched default strings");
	const targetPayload = JSON.parse(mapCall.args.target_doc);
	assert.equal(targetPayload.items.length, 0, "Virgin row with string defaults pruned in payload");
	assert.equal(frm.doc.items.length, 1, "Original doc untouched before RPC response");
});

test("Target safety: rejects target with arbitrary string '0' in non-numeric field like batch_no without mutation or RPC", () => {
	const ctx = createFrappeEnvironment();
	loadScriptInVm(ctx);

	const frm = new ctx.FormStub("Sales Order", {
		docstatus: 0,
		company: "Company B",
		customer: "Cust-A",
		// User named batch_no "0" on a non-numeric field
		items: [{ doctype: "Sales Order Item", item_code: "", batch_no: "0" }],
	});

	triggerEvent(ctx, "Sales Order", "refresh", frm);
	frm.page.inner_buttons["Get Items From"][0].action();

	const dialog = ctx.getLastDialog();
	assert.equal(dialog, null, "Dialog must not open when target row has non-numeric batch_no '0'");

	const mapCalls = ctx.calls.filter((c) => c.method?.includes("make_sales_order"));
	assert.equal(mapCalls.length, 0, "Must not dispatch RPC when row has batch_no '0'");
	assert.equal(frm.doc.items.length, 1);
	assert.equal(frm.doc.items[0].batch_no, "0", "User batch_no '0' must be preserved");
	assert.ok(
		ctx.messages.some((m) => (typeof m === "string" ? m : m.message).match(/sudah berisi item/i)),
		"User must receive notification that target is already occupied"
	);
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
