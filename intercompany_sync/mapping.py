import json

import frappe
from frappe import _
from frappe.utils import cint, flt


def _validate_target_doc(target_doc, doctype: str, source_name: str, expected_company: str) -> str | None:
	"""Validate target document, verifying persisted status if existing; return verified target name."""
	if not target_doc:
		return None

	if isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	if target_doc.get("items"):
		frappe.throw(
			_("Dokumen target sudah berisi item. Tidak dapat memetakan ke target yang sudah terisi."),
			title=_("Target Tidak Kosong"),
		)

	ref_field = (
		"inter_company_order_reference"
		if doctype == "Sales Order"
		else "inter_company_reference"
	)
	target_ref = target_doc.get(ref_field)
	if target_ref and target_ref != source_name:
		frappe.throw(
			_("Dokumen target sudah terhubung ke {0}. Tidak dapat mencampur sumber inter-company.").format(
				target_ref
			),
			title=_("Sumber Campuran"),
		)

	if target_doc.get("company") and target_doc.company != expected_company:
		frappe.throw(
			_("Company dokumen target ({0}) tidak sesuai dengan transaksi inter-company ({1}).").format(
				target_doc.company, expected_company
			),
			title=_("Company Tidak Sesuai"),
		)

	raw_name = target_doc.get("name")
	is_local = target_doc.get("__islocal") or (
		callable(getattr(target_doc, "is_new", None)) and target_doc.is_new()
	)
	if raw_name and not is_local:
		if not frappe.db.exists(doctype, raw_name):
			frappe.throw(
				_("Dokumen target {0} tidak ditemukan di database.").format(raw_name),
				title=_("Target Tidak Ditemukan"),
			)
		persisted = frappe.get_doc(doctype, raw_name)
		if not frappe.has_permission(doctype, "read", persisted) or not frappe.has_permission(
			doctype, "write", persisted
		):
			raise frappe.PermissionError(
				_("Anda tidak memiliki izin membaca atau mengubah {0} {1}.").format(doctype, raw_name)
			)
		if persisted.docstatus != 0:
			frappe.throw(
				_("Dokumen target {0} tidak dapat diubah karena berstatus {1} (wajib draft).").format(
					raw_name, persisted.docstatus
				),
				title=_("Target Bukan Draf"),
			)
		if persisted.get("items"):
			frappe.throw(
				_("Dokumen target {0} sudah berisi item di database.").format(raw_name),
				title=_("Target Tidak Kosong"),
			)
		if persisted.company != expected_company:
			frappe.throw(
				_("Company dokumen target ({0}) tidak sesuai dengan transaksi inter-company ({1}).").format(
					persisted.company, expected_company
				),
				title=_("Company Tidak Sesuai"),
			)
		persisted_ref = persisted.get(ref_field)
		if persisted_ref and persisted_ref != source_name:
			frappe.throw(
				_("Dokumen target sudah terhubung ke {0}. Tidak dapat mencampur sumber inter-company.").format(
					persisted_ref
				),
				title=_("Sumber Campuran"),
			)
		return raw_name

	return None


@frappe.whitelist()
def make_sales_order(source_name: str, target_doc=None):
	"""Create a draft Sales Order from a submitted inter-company Purchase Order."""
	if not frappe.has_permission("Purchase Order", "read", source_name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca Purchase Order {0}.").format(source_name)
		)
	if not frappe.has_permission("Sales Order", "create"):
		raise frappe.PermissionError(_("Anda tidak memiliki izin membuat Sales Order."))

	source_doc = frappe.get_doc("Purchase Order", source_name)
	if not source_doc:
		frappe.throw(_("Purchase Order {0} tidak ditemukan.").format(source_name))

	if source_doc.docstatus != 1:
		frappe.throw(
			_("Purchase Order {0} harus dalam status tersubmit (docstatus=1).").format(source_name)
		)

	expected_co = frappe.get_cached_value("Supplier", source_doc.supplier, "represents_company")
	is_internal_supp = frappe.get_cached_value("Supplier", source_doc.supplier, "is_internal_supplier")
	if not is_internal_supp or not expected_co:
		frappe.throw(
			_("Supplier {0} bukan internal supplier untuk transaksi antar-company.").format(
				source_doc.supplier
			),
			title=_("Party Tidak Sesuai"),
		)

	if isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	# Capture requested customer scalar before native mapper mutates target_doc in place
	supplied_customer = target_doc.get("customer") if target_doc else None

	verified_target_name = _validate_target_doc(target_doc, "Sales Order", source_name, expected_co)

	so_filters = {
		"inter_company_order_reference": source_name,
		"docstatus": ("<", 2),
	}
	if verified_target_name:
		so_filters["name"] = ("!=", verified_target_name)

	existing_so = frappe.db.get_value("Sales Order", so_filters, "name")
	source_paired = getattr(source_doc, "inter_company_order_reference", None)
	if source_paired and verified_target_name and source_paired == verified_target_name:
		source_paired = None

	paired_name = existing_so or source_paired
	if paired_name:
		if frappe.has_permission("Sales Order", "read", paired_name):
			msg = _("Sales Order {0} sudah dibuat untuk Purchase Order {1}.").format(
				paired_name, source_name
			)
		else:
			msg = _("Sales Order sudah dibuat untuk Purchase Order {0}.").format(source_name)
		frappe.throw(msg, title=_("Dokumen Sudah Ada"))

	from erpnext.buying.doctype.purchase_order.purchase_order import (
		make_inter_company_sales_order as native_make_sales_order,
	)

	so = native_make_sales_order(source_name, target_doc=target_doc)

	if supplied_customer and so.customer != supplied_customer:
		frappe.throw(
			_("Customer dokumen target ({0}) tidak sesuai dengan hasil pemetaan native ({1}).").format(
				supplied_customer, so.customer
			),
			title=_("Party Tidak Sesuai"),
		)

	so.inter_company_order_reference = source_name
	if so.get("inter_company_invoice_reference"):
		so.inter_company_invoice_reference = None

	source_item_names = {d.name for d in source_doc.get("items", [])}
	for item in so.get("items", []):
		item.purchase_order = source_name
		if not item.get("purchase_order_item") or item.purchase_order_item not in source_item_names:
			frappe.throw(
				_(
					"Baris item {0} pada Sales Order tidak memiliki referensi baris Purchase Order yang valid ({1})."
				).format(
					item.get("item_code") or item.get("idx") or "",
					item.get("purchase_order_item") or "",
				),
				title=_("Referensi Baris Tidak Valid"),
			)

	so.docstatus = 0
	return so


def get_outstanding_dn_rows(
	delivery_note,
	exclude_pr: str | None = None,
	for_update: bool = False,
) -> dict:
	"""Calculate outstanding stock and transaction qty per Delivery Note Item row."""
	if isinstance(delivery_note, str):
		dn = frappe.get_doc("Delivery Note", delivery_note)
	else:
		dn = delivery_note

	dn_items = dn.get("items", [])
	if not dn_items:
		return {}

	dn_item_names = [
		(d.get("name") if isinstance(d, dict) else getattr(d, "name", None))
		for d in dn_items
		if (d.get("name") if isinstance(d, dict) else getattr(d, "name", None))
	]
	filters = {
		"delivery_note_item": ("in", dn_item_names),
		"docstatus": 1,
	}

	if for_update and hasattr(frappe.db, "get_values"):
		pr_items = frappe.db.get_values(
			"Purchase Receipt Item",
			filters=filters,
			fieldname=[
				"delivery_note_item",
				"qty",
				"rejected_qty",
				"conversion_factor",
				"received_stock_qty",
				"parent",
			],
			as_dict=True,
			for_update=True,
		) or []
	else:
		pr_items = frappe.get_all(
			"Purchase Receipt Item",
			filters=filters,
			fields=[
				"delivery_note_item",
				"qty",
				"rejected_qty",
				"conversion_factor",
				"received_stock_qty",
				"parent",
			],
		)

	received_by_dni: dict[str, float] = {}
	for pr_item in pr_items:
		if exclude_pr and pr_item.get("parent") == exclude_pr:
			continue
		dni = pr_item.get("delivery_note_item")
		if not dni:
			continue

		cf = flt(pr_item.get("conversion_factor") or 1.0)
		rec_stock = flt(pr_item.get("received_stock_qty"))
		if not rec_stock:
			total_rec_qty = flt(pr_item.get("qty")) + flt(pr_item.get("rejected_qty"))
			rec_stock = total_rec_qty * cf

		received_by_dni[dni] = received_by_dni.get(dni, 0.0) + rec_stock

	result: dict[str, dict] = {}
	for dn_item in dn_items:
		dni = dn_item.get("name") if isinstance(dn_item, dict) else getattr(dn_item, "name", None)
		cf = flt(
			(dn_item.get("conversion_factor") if isinstance(dn_item, dict) else getattr(dn_item, "conversion_factor", None))
			or 1.0
		)
		dispatched_qty = flt(
			dn_item.get("qty") if isinstance(dn_item, dict) else getattr(dn_item, "qty", 0.0)
		)
		raw_stock = (
			dn_item.get("stock_qty") if isinstance(dn_item, dict) else getattr(dn_item, "stock_qty", None)
		)
		dispatched_stock = flt(raw_stock) if raw_stock is not None else (dispatched_qty * cf)

		already_received_stock = flt(received_by_dni.get(dni, 0.0))
		outstanding_stock = max(0.0, flt(dispatched_stock - already_received_stock, 6))
		outstanding_qty = max(0.0, flt(outstanding_stock / cf, 6)) if cf else 0.0

		result[dni] = {
			"qty": outstanding_qty,
			"stock_qty": outstanding_stock,
			"dispatched_qty": dispatched_qty,
			"dispatched_stock_qty": dispatched_stock,
			"received_stock_qty": already_received_stock,
			"conversion_factor": cf,
			"item_code": dn_item.get("item_code") if isinstance(dn_item, dict) else getattr(dn_item, "item_code", None),
			"uom": dn_item.get("uom") if isinstance(dn_item, dict) else getattr(dn_item, "uom", None),
			"stock_uom": dn_item.get("stock_uom") if isinstance(dn_item, dict) else getattr(dn_item, "stock_uom", None),
		}

	return result


@frappe.whitelist()
def make_purchase_receipt(source_name: str, target_doc=None):
	"""Create a draft Purchase Receipt offering outstanding quantities from a Delivery Note."""
	if not frappe.has_permission("Delivery Note", "read", source_name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca Delivery Note {0}.").format(source_name)
		)
	if not frappe.has_permission("Purchase Receipt", "create"):
		raise frappe.PermissionError(_("Anda tidak memiliki izin membuat Purchase Receipt."))

	source_doc = frappe.get_doc("Delivery Note", source_name)
	if not source_doc:
		frappe.throw(_("Delivery Note {0} tidak ditemukan.").format(source_name))

	if source_doc.docstatus != 1:
		frappe.throw(
			_("Delivery Note {0} harus dalam status tersubmit (docstatus=1).").format(source_name)
		)

	if getattr(source_doc, "is_return", False):
		frappe.throw(_("Delivery Note {0} adalah retur dan tidak dapat dipetakan.").format(source_name))

	expected_co = frappe.get_cached_value("Customer", source_doc.customer, "represents_company")
	is_internal_cust = frappe.get_cached_value("Customer", source_doc.customer, "is_internal_customer")
	if not is_internal_cust or not expected_co:
		frappe.throw(
			_("Customer {0} bukan internal customer untuk transaksi antar-company.").format(
				source_doc.customer
			),
			title=_("Party Tidak Sesuai"),
		)

	if isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	# Capture requested supplier scalar before native mapper mutates target_doc in place
	supplied_supplier = target_doc.get("supplier") if target_doc else None

	_validate_target_doc(target_doc, "Purchase Receipt", source_name, expected_co)

	outstanding_map = get_outstanding_dn_rows(source_doc)
	total_outstanding_stock = sum(row["stock_qty"] for row in outstanding_map.values())
	if flt(total_outstanding_stock, 4) <= 0:
		frappe.throw(
			_("Semua item dalam Delivery Note {0} sudah diterima.").format(source_name),
			title=_("Sudah Diterima"),
		)

	from erpnext.stock.doctype.delivery_note.delivery_note import (
		make_inter_company_purchase_receipt as native_make_purchase_receipt,
	)

	pr = native_make_purchase_receipt(source_name, target_doc=target_doc)

	if supplied_supplier and pr.supplier != supplied_supplier:
		frappe.throw(
			_("Supplier dokumen target ({0}) tidak sesuai dengan hasil pemetaan native ({1}).").format(
				supplied_supplier, pr.supplier
			),
			title=_("Party Tidak Sesuai"),
		)

	pr.inter_company_reference = source_name
	pr.supplier_delivery_note = source_name

	conversion_rate = flt(getattr(pr, "conversion_rate", None) or 1.0)
	kept_items = []
	for item in pr.get("items", []):
		dni = item.get("delivery_note_item")
		row_info = outstanding_map.get(dni)
		if not row_info or row_info["stock_qty"] <= 0:
			continue

		cf = flt(item.get("conversion_factor") or row_info.get("conversion_factor") or 1.0)
		stock_prec = (
			item.precision("stock_qty")
			if callable(getattr(item, "precision", None))
			else None
		)
		qty_prec = (
			item.precision("qty")
			if callable(getattr(item, "precision", None))
			else None
		)
		rate_prec = (
			item.precision("rate")
			if callable(getattr(item, "precision", None))
			else None
		)
		amount_prec = (
			item.precision("amount")
			if callable(getattr(item, "precision", None))
			else None
		)
		base_amount_prec = (
			item.precision("base_amount")
			if callable(getattr(item, "precision", None))
			else None
		)

		item.stock_qty = flt(row_info["stock_qty"], stock_prec)
		item.qty = flt(row_info["qty"], qty_prec)
		rate = flt(item.get("rate"), rate_prec)
		item.amount = flt(item.qty * rate, amount_prec)
		item.base_amount = flt(item.amount * conversion_rate, base_amount_prec)

		rejected_qty = flt(item.get("rejected_qty") or 0.0)
		item.received_qty = flt(item.qty + rejected_qty, qty_prec)
		item.received_stock_qty = flt(item.received_qty * cf, stock_prec)

		if not item.get("purchase_order") or not item.get("purchase_order_item"):
			dn_item = frappe.db.get_value(
				"Delivery Note Item",
				dni,
				["purchase_order", "purchase_order_item", "so_detail"],
				as_dict=True,
			)
			if dn_item:
				if dn_item.get("purchase_order") and dn_item.get("purchase_order_item"):
					item.purchase_order = dn_item["purchase_order"]
					item.purchase_order_item = dn_item["purchase_order_item"]
				elif dn_item.get("so_detail"):
					so_item = frappe.db.get_value(
						"Sales Order Item",
						dn_item["so_detail"],
						["purchase_order", "purchase_order_item"],
						as_dict=True,
					)
					if so_item and so_item.get("purchase_order") and so_item.get("purchase_order_item"):
						item.purchase_order = so_item["purchase_order"]
						item.purchase_order_item = so_item["purchase_order_item"]

		kept_items.append(item)

	pr.items = kept_items
	if not pr.items:
		frappe.throw(
			_("Semua item dalam Delivery Note {0} sudah diterima.").format(source_name),
			title=_("Sudah Diterima"),
		)

	if callable(getattr(pr, "calculate_taxes_and_totals", None)):
		pr.calculate_taxes_and_totals()
	elif callable(getattr(pr, "run_method", None)):
		pr.run_method("calculate_taxes_and_totals")

	pr.docstatus = 0
	return pr


@frappe.whitelist()
def get_sources(
	source_doctype: str,
	target_company: str,
	target_party: str | None = None,
	txt: str = "",
	start: int = 0,
	page_length: int = 20,
) -> list[dict]:
	"""Return eligible, permitted source documents using native resolver and bounded scanning."""
	if source_doctype not in ("Purchase Order", "Delivery Note"):
		frappe.throw(
			_("Tipe dokumen sumber tidak didukung: {0}").format(source_doctype),
			title=_("Tipe Tidak Didukung"),
		)

	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_inter_company_details

	start_idx = max(0, cint(start))
	limit_len = min(max(1, cint(page_length) or 20), 100)
	needed = start_idx + limit_len
	chunk_size = 50
	max_scan = 500
	order_by = "modified desc, name desc"

	or_filters = None
	if txt and txt.strip():
		txt_pattern = f"%{txt.strip()}%"
		or_filters = [
			["name", "like", txt_pattern],
			["company", "like", txt_pattern],
		]

	results = []
	offset = 0

	if source_doctype == "Purchase Order":
		filters = [["docstatus", "=", 1]]
		while len(results) < needed and offset < max_scan:
			cur_chunk = min(chunk_size, max_scan - offset)
			batch = frappe.get_list(
				"Purchase Order",
				filters=filters,
				or_filters=or_filters,
				fields=[
					"name",
					"company",
					"supplier",
					"transaction_date",
					"grand_total",
					"currency",
					"inter_company_order_reference",
					"shipping_address",
				],
				order_by=order_by,
				start=offset,
				page_length=cur_chunk,
			)
			if not batch:
				break

			offset += len(batch)
			unpaired = [p for p in batch if not p.get("inter_company_order_reference")]
			paired_set = set()
			if unpaired:
				existing = frappe.db.get_all(
					"Sales Order",
					filters={
						"inter_company_order_reference": ("in", [p.name for p in unpaired]),
						"docstatus": ("<", 2),
					},
					pluck="inter_company_order_reference",
				)
				paired_set = set(existing)

			for p in batch:
				if p.get("inter_company_order_reference") or p.name in paired_set:
					continue
				try:
					details = get_inter_company_details(p, "Purchase Order")
				except (frappe.ValidationError, KeyError):
					continue
				if details.get("company") != target_company:
					continue
				if target_party and details.get("party") != target_party:
					continue

				results.append(p)
				if len(results) >= needed:
					break

	else:
		filters = [["docstatus", "=", 1], ["is_return", "=", 0]]
		while len(results) < needed and offset < max_scan:
			cur_chunk = min(chunk_size, max_scan - offset)
			batch = frappe.get_list(
				"Delivery Note",
				filters=filters,
				or_filters=or_filters,
				fields=[
					"name",
					"company",
					"customer",
					"posting_date",
					"grand_total",
					"currency",
					"company_address",
					"shipping_address",
				],
				order_by=order_by,
				start=offset,
				page_length=cur_chunk,
			)
			if not batch:
				break

			offset += len(batch)
			for d in batch:
				try:
					details = get_inter_company_details(d, "Delivery Note")
				except (frappe.ValidationError, KeyError):
					continue
				if details.get("company") != target_company:
					continue
				if target_party and details.get("party") != target_party:
					continue

				out_map = get_outstanding_dn_rows(d.name)
				if any(row["stock_qty"] > 0 for row in out_map.values()):
					results.append(d)
					if len(results) >= needed:
						break

	if offset >= max_scan and len(results) < needed:
		frappe.msgprint(
			_("Pencarian mencapai batas 500 dokumen. Harap persempit pencarian menggunakan filter teks."),
			alert=True,
		)

	return results[start_idx : start_idx + limit_len]
