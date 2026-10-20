import frappe
from frappe import _


def validate_purchase_receipt(doc, method=None) -> None:
	"""Enforce that inter-company PR must originate from a Delivery Note."""
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

	pr_name = frappe.db.get_value(
		"Purchase Receipt",
		{"inter_company_reference": doc.name, "docstatus": 1},
		"name",
	)
	if pr_name:
		frappe.throw(
			_("Delivery Note sudah diterima lewat Purchase Receipt {0}.").format(pr_name),
			title=_("Purchase Receipt Aktif"),
		)

