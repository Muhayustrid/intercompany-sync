import math

import frappe
from frappe import _
from frappe.utils import flt

CF_PRECISION = 9  # Native precision for conversion_factor in DN Item and PR Item schemas


def _validate_finite_num(val, fieldname: str, idx: int, allow_zero: bool = True) -> float:
	"""Validate that a numeric value is finite, real, and non-negative (or positive)."""
	try:
		num = float(val)
	except (TypeError, ValueError):
		frappe.throw(
			_("Baris #{0}: Nilai {1} tidak valid.").format(idx, fieldname),
			title=_("Kuantitas Tidak Valid"),
		)
	if math.isnan(num) or math.isinf(num):
		frappe.throw(
			_("Baris #{0}: Nilai {1} harus berupa angka terhingga.").format(idx, fieldname),
			title=_("Kuantitas Tidak Valid"),
		)
	if allow_zero and num < 0:
		frappe.throw(
			_("Baris #{0}: Nilai {1} tidak boleh negatif.").format(idx, fieldname),
			title=_("Kuantitas Tidak Valid"),
		)
	elif not allow_zero and num <= 0:
		frappe.throw(
			_("Baris #{0}: Nilai {1} harus lebih besar dari 0.").format(idx, fieldname),
			title=_("Kuantitas Tidak Valid"),
		)
	return num


def _lock_and_get_dn(dn_name: str) -> dict:
	"""Acquire deterministic row locks on Delivery Note and items and return current data."""
	db_type = getattr(frappe.db, "db_type", "mariadb")
	for_update = "FOR UPDATE" if db_type != "sqlite" else ""

	header_rows = frappe.db.sql(
		f"SELECT name, docstatus, is_return, customer, company FROM `tabDelivery Note` WHERE name = %s {for_update}",
		(dn_name,),
		as_dict=True,
	)
	if not header_rows:
		frappe.throw(
			_("Delivery Note {0} tidak ditemukan.").format(dn_name),
			title=_("Status Dokumen Tidak Valid"),
		)

	item_rows = frappe.db.sql(
		f"SELECT name, parent, item_code, uom, stock_uom, conversion_factor, qty, stock_qty "
		f"FROM `tabDelivery Note Item` WHERE parent = %s ORDER BY name ASC {for_update}",
		(dn_name,),
		as_dict=True,
	)

	dn_dict = dict(header_rows[0])
	dn_dict["items"] = item_rows or []
	return dn_dict


def _validate_pr_against_dn_data(doc, dn, check_original_capacity: bool = False) -> dict[str, float]:
	"""Validate PR structure, row references, UOMs, and conversion factors against DN data."""
	dn_name = dn.get("name") or doc.inter_company_reference

	if dn.get("docstatus") != 1:
		frappe.throw(
			_("Delivery Note {0} harus dalam status tersubmit (docstatus=1).").format(dn_name),
			title=_("Status Dokumen Tidak Valid"),
		)

	if dn.get("is_return"):
		frappe.throw(
			_("Delivery Note {0} adalah dokumen retur.").format(dn_name),
			title=_("Status Dokumen Tidak Valid"),
		)

	# Validate party and company representations
	dn_customer_co = frappe.get_cached_value("Customer", dn.get("customer"), "represents_company")
	if dn_customer_co != doc.company:
		frappe.throw(
			_(
				"Customer {0} pada Delivery Note {1} mewakili company {2}, "
				"tetapi Purchase Receipt dibuat untuk company {3}."
			).format(dn.get("customer"), dn_name, dn_customer_co, doc.company),
			title=_("Party Tidak Sesuai"),
		)

	pr_supplier_co = frappe.get_cached_value("Supplier", doc.supplier, "represents_company")
	if pr_supplier_co != dn.get("company"):
		frappe.throw(
			_(
				"Supplier {0} pada Purchase Receipt mewakili company {1}, "
				"tetapi Delivery Note {2} dibuat oleh company {3}."
			).format(doc.supplier, pr_supplier_co, dn_name, dn.get("company")),
			title=_("Party Tidak Sesuai"),
		)

	if not doc.items:
		frappe.throw(
			_("Purchase Receipt wajib memiliki item."),
			title=_("Item Tidak Valid"),
		)

	dn_items_by_name = {d.get("name"): d for d in dn.get("items", []) if d.get("name")}
	aggregate_requested_stock: dict[str, float] = {}

	for item in doc.items:
		idx = item.idx or 1

		# Optional item.delivery_note parent check: if provided, must match header
		item_dn = getattr(item, "delivery_note", None)
		if item_dn and item_dn != doc.inter_company_reference:
			frappe.throw(
				_(
					"Baris #{0}: Delivery Note pada baris ({1}) tidak sesuai dengan "
					"Delivery Note pada header ({2})."
				).format(idx, item_dn, doc.inter_company_reference),
				title=_("Referensi Dokumen Tidak Sesuai"),
			)

		# Warehouse validation
		if not item.warehouse:
			frappe.throw(
				_("Baris #{0}: Warehouse wajib diisi.").format(idx),
				title=_("Warehouse Tidak Sesuai"),
			)
		wh_company = frappe.db.get_value("Warehouse", item.warehouse, "company")
		if wh_company and wh_company != doc.company:
			frappe.throw(
				_("Baris #{0}: Warehouse {1} bukan milik company {2}.").format(
					idx, item.warehouse, doc.company
				),
				title=_("Warehouse Tidak Sesuai"),
			)

		# Exact DN Item row reference
		dni = item.delivery_note_item
		if not dni:
			frappe.throw(
				_("Baris #{0}: Item {1} wajib memiliki referensi baris Delivery Note (delivery_note_item).").format(
					idx, item.item_code or item.item_name
				),
				title=_("Referensi Baris Tidak Valid"),
			)
		if dni not in dn_items_by_name:
			frappe.throw(
				_("Baris #{0}: Referensi baris Delivery Note ({1}) tidak ditemukan pada Delivery Note {2}.").format(
					idx, dni, dn_name
				),
				title=_("Referensi Baris Tidak Valid"),
			)

		dn_item = dn_items_by_name[dni]

		# Item code match
		if item.item_code != dn_item.get("item_code"):
			frappe.throw(
				_("Baris #{0}: Item code {1} tidak sesuai dengan baris Delivery Note ({2}).").format(
					idx, item.item_code, dn_item.get("item_code")
				),
				title=_("Item Tidak Sesuai"),
			)

		# Stock UOM match
		if item.stock_uom != dn_item.get("stock_uom"):
			frappe.throw(
				_("Baris #{0}: Stock UOM {1} tidak sesuai dengan Stock UOM Delivery Note ({2}).").format(
					idx, item.stock_uom, dn_item.get("stock_uom")
				),
				title=_("UOM Tidak Sesuai"),
			)

		# Transaction UOM restriction: must match DN transaction UOM to protect native status updater
		if item.uom != dn_item.get("uom"):
			frappe.throw(
				_(
					"Baris #{0}: UOM {1} harus sama dengan UOM pada Delivery Note ({2}) "
					"untuk menjaga konsistensi penerimaan status updater."
				).format(idx, item.uom, dn_item.get("uom")),
				title=_("UOM Tidak Sesuai"),
			)

		# Numeric validation
		qty = _validate_finite_num(item.qty, "qty", idx, allow_zero=True)
		rej_val = 0.0 if getattr(item, "rejected_qty", None) is None else item.rejected_qty
		rej_qty = _validate_finite_num(rej_val, "rejected_qty", idx, allow_zero=True)
		cf_val = 1.0 if getattr(item, "conversion_factor", None) is None else item.conversion_factor
		pr_cf = _validate_finite_num(cf_val, "conversion_factor", idx, allow_zero=False)

		# Conversion factor must equal DN conversion factor at native precision 9
		dn_cf_val = 1.0 if dn_item.get("conversion_factor") is None else dn_item.get("conversion_factor")
		dn_cf = _validate_finite_num(dn_cf_val, "conversion_factor DN", idx, allow_zero=False)
		if flt(pr_cf, CF_PRECISION) != flt(dn_cf, CF_PRECISION):
			frappe.throw(
				_(
					"Baris #{0}: Conversion factor ({1}) harus sama dengan "
					"conversion factor pada Delivery Note ({2})."
				).format(idx, pr_cf, dn_cf),
				title=_("Faktor Konversi Tidak Sesuai"),
			)

		if qty + rej_qty <= 0:
			frappe.throw(
				_("Baris #{0}: Total penerimaan (qty + rejected_qty) harus lebih besar dari 0.").format(idx),
				title=_("Kuantitas Tidak Valid"),
			)

		# Linked PO UOM match
		if getattr(item, "purchase_order_item", None):
			po_uom = frappe.db.get_value("Purchase Order Item", item.purchase_order_item, "uom")
			if po_uom and item.uom != po_uom:
				frappe.throw(
					_("Baris #{0}: UOM {1} tidak sesuai dengan UOM pada Purchase Order Item ({2}).").format(
						idx, item.uom, po_uom
					),
					title=_("UOM Tidak Sesuai"),
				)

		stock_units = (qty + rej_qty) * pr_cf
		aggregate_requested_stock[dni] = aggregate_requested_stock.get(dni, 0.0) + stock_units

	# Finding 4: Draft validate checks aggregate requested <= original dispatched stock on DN
	if check_original_capacity:
		for dni, requested_stock in aggregate_requested_stock.items():
			dn_item = dn_items_by_name[dni]
			dn_cf = flt(dn_item.get("conversion_factor") or 1.0)
			dispatched_stock = flt(dn_item.get("stock_qty")) or (flt(dn_item.get("qty")) * dn_cf)
			if flt(requested_stock - dispatched_stock, 6) > 1e-6:
				frappe.throw(
					_(
						"Kuantitas penerimaan untuk item {0} ({1} {2}) melebihi "
						"kuantitas pengiriman Delivery Note ({3} {2})."
					).format(
						dn_item.get("item_code"),
						flt(requested_stock, 4),
						dn_item.get("stock_uom"),
						flt(dispatched_stock, 4),
					),
					title=_("Kuantitas Melebihi Pengiriman"),
				)

	return aggregate_requested_stock


def validate_purchase_receipt(doc, method=None) -> None:
	"""Enforce that inter-company PR originates from a valid submitted Delivery Note with exact references."""
	if doc.is_return:
		return

	is_internal = doc.is_internal_supplier or (
		doc.supplier and frappe.db.get_value("Supplier", doc.supplier, "is_internal_supplier")
	)
	if not is_internal:
		return

	if not doc.inter_company_reference:
		frappe.throw(
			_(
				"Purchase Receipt untuk supplier internal wajib memiliki referensi "
				"Delivery Note (inter_company_reference)."
			),
			title=_("Referensi Inter-company Kurang"),
		)

	dn_name = doc.inter_company_reference
	if not frappe.has_permission("Delivery Note", "read", doc=dn_name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca Delivery Note referensi.")
		)

	dn = frappe.get_doc("Delivery Note", dn_name)
	if not dn:
		frappe.throw(
			_("Delivery Note {0} tidak ditemukan.").format(dn_name),
			title=_("Status Dokumen Tidak Valid"),
		)

	# Validate against DN and check original dispatched capacity for draft
	_validate_pr_against_dn_data(doc, dn, check_original_capacity=True)


def before_submit_purchase_receipt(doc, method=None) -> None:
	"""Lock referenced Delivery Note and enforce partial receipt quantity limits in stock units."""
	if doc.is_return:
		return

	is_internal = doc.is_internal_supplier or (
		doc.supplier and frappe.db.get_value("Supplier", doc.supplier, "is_internal_supplier")
	)
	if not is_internal:
		return

	dn_name = doc.inter_company_reference
	if not dn_name:
		frappe.throw(
			_(
				"Purchase Receipt untuk supplier internal wajib memiliki referensi "
				"Delivery Note (inter_company_reference)."
			),
			title=_("Referensi Inter-company Kurang"),
		)

	if not frappe.has_permission("Delivery Note", "read", doc=dn_name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca Delivery Note referensi.")
		)

	# 1. Acquire deterministic lock on Delivery Note parent and item rows and get current data
	locked_dn = _lock_and_get_dn(dn_name)

	# 2. Revalidate PR against locked DN data
	current_pr_stock_by_dni = _validate_pr_against_dn_data(doc, locked_dn, check_original_capacity=False)

	from intercompany_sync.mapping import get_outstanding_dn_rows

	# 3. Read current outstanding stock AFTER taking lock (for_update=True) using locked DN data
	outstanding_map = get_outstanding_dn_rows(locked_dn, exclude_pr=doc.name, for_update=True)
	for dni, requested_stock in current_pr_stock_by_dni.items():
		if dni not in outstanding_map:
			continue
		row_info = outstanding_map[dni]
		available_stock = row_info["stock_qty"]
		if flt(requested_stock - available_stock, 6) > 1e-6:
			frappe.throw(
				_(
					"Kuantitas penerimaan untuk item {0} ({1} {2}) melebihi sisa Delivery Note {3} ({4} {2})."
				).format(
					row_info.get("item_code"),
					flt(requested_stock, 4),
					row_info.get("stock_uom"),
					dn_name,
					flt(available_stock, 4),
				),
				title=_("Kuantitas Melebihi Sisa"),
			)


def validate_sales_invoice(doc, method=None) -> None:
	"""Enforce that inter-company SI must originate from DN and outlet must have submitted PR."""
	if doc.is_return or doc.is_pos:
		return

	is_internal = doc.is_internal_customer or (
		doc.customer and frappe.db.get_value("Customer", doc.customer, "is_internal_customer")
	)
	if not is_internal:
		return

	if doc.amended_from and doc.inter_company_invoice_reference:
		doc.inter_company_invoice_reference = None

	if not doc.items:
		frappe.throw(
			_("Sales Invoice antar-company wajib memiliki item dari Delivery Note."),
			title=_("Delivery Note Kurang"),
		)

	dn_names = set()
	for item in doc.items:
		if not item.dn_detail:
			frappe.throw(
				_("Baris #{0}: Item {1} wajib berasal dari Delivery Note (dn_detail kosong).").format(
					item.idx, item.item_code or item.item_name
				),
				title=_("Delivery Note Kurang"),
			)
		dn = item.delivery_note or frappe.db.get_value("Delivery Note Item", item.dn_detail, "parent")
		if dn:
			dn_names.add(dn)

	for dn_name in sorted(dn_names):
		has_submitted_pr = frappe.db.exists(
			"Purchase Receipt",
			{
				"inter_company_reference": dn_name,
				"docstatus": 1,
			},
		)
		if not has_submitted_pr:
			frappe.throw(
				_(
					"Delivery Note {0} belum memiliki Purchase Receipt yang tersubmit (docstatus=1) di outlet."
				).format(dn_name),
				title=_("Purchase Receipt Belum Ada"),
			)


def validate_purchase_invoice_cancel(doc, method=None) -> None:
	"""Enforce that inter-company Purchase Invoice can only be cancelled via Sales Invoice."""
	if getattr(doc.flags, "ic_cascade", False):
		return

	if not doc.inter_company_invoice_reference:
		return

	si_status = frappe.db.get_value(
		"Sales Invoice", doc.inter_company_invoice_reference, "docstatus"
	)
	if si_status == 1:
		frappe.throw(
			_("Batalkan lewat Sales Invoice {0}.").format(doc.inter_company_invoice_reference),
			title=_("Batalkan Lewat Sales Invoice"),
		)


def validate_delivery_note_cancel(doc, method=None) -> None:
	"""Enforce that inter-company Delivery Note cannot be cancelled if counterpart PR is submitted."""
	if doc.is_return:
		return

	is_internal = doc.is_internal_customer or (
		doc.customer and frappe.db.get_value("Customer", doc.customer, "is_internal_customer")
	)
	if not is_internal:
		return

	# 1. Lock the same Delivery Note parent row
	db_type = getattr(frappe.db, "db_type", "mariadb")
	for_update = "FOR UPDATE" if db_type != "sqlite" else ""

	if for_update:
		frappe.db.sql(
			f"SELECT name FROM `tabDelivery Note` WHERE name = %s {for_update}",
			(doc.name,),
		)

	# 2. Current read of counterpart submitted PRs (LIMIT 1 must precede FOR UPDATE in MariaDB)
	lock_clause = f" {for_update}" if for_update else ""
	pr_rows = frappe.db.sql(
		f"SELECT name FROM `tabPurchase Receipt` WHERE inter_company_reference = %s AND docstatus = 1 LIMIT 1{lock_clause}",
		(doc.name,),
		as_dict=True,
	)
	if pr_rows:
		pr_name = pr_rows[0].get("name")
		frappe.throw(
			_("Delivery Note sudah diterima lewat Purchase Receipt {0}.").format(pr_name),
			title=_("Purchase Receipt Aktif"),
		)

