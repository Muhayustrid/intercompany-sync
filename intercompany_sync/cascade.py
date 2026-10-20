import frappe
from frappe import _


def _add_ignore_linked_doctype(doc, target_doctype: str) -> None:
	"""Add target_doctype to doc.ignore_linked_doctypes to bypass backlink check on cancel."""
	existing = getattr(doc, "ignore_linked_doctypes", None)
	if existing:
		if isinstance(existing, (list, tuple)):
			existing_list = list(existing)
			if target_doctype not in existing_list:
				existing_list.append(target_doctype)
			doc.ignore_linked_doctypes = tuple(existing_list)
	else:
		doc.ignore_linked_doctypes = (target_doctype,)


def before_cancel_sales_invoice(doc, method=None) -> None:
	"""Cascade cancellation to counterpart Purchase Invoice or block if already paid."""
	if doc.is_return:
		return

	is_internal = doc.is_internal_customer or (
		doc.customer and frappe.db.get_value("Customer", doc.customer, "is_internal_customer")
	)
	if not is_internal:
		return

	pi_name = frappe.db.get_value(
		"Purchase Invoice",
		{"inter_company_invoice_reference": doc.name, "docstatus": ("<", 2)},
		"name",
	)
	if (
		not pi_name
		and doc.inter_company_invoice_reference
		and frappe.db.exists(
			"Purchase Invoice",
			{"name": doc.inter_company_invoice_reference, "docstatus": ("<", 2)},
		)
	):
		pi_name = doc.inter_company_invoice_reference

	if not pi_name:
		return

	pi_doc = frappe.get_doc("Purchase Invoice", pi_name)
	if float(pi_doc.outstanding_amount or 0.0) < float(pi_doc.grand_total or 0.0):
		frappe.throw(
			_("Purchase Invoice {0} sudah dibayar. Batalkan pembayarannya dulu.").format(pi_name),
			title=_("Purchase Invoice Sudah Dibayar"),
		)

	orig_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		if pi_doc.docstatus == 1:
			pi_doc.flags.ignore_permissions = True
			pi_doc.flags.ic_cascade = True
			pi_doc.cancel()
		elif pi_doc.docstatus == 0:
			frappe.delete_doc("Purchase Invoice", pi_doc.name, force=1, ignore_permissions=True)
	finally:
		frappe.set_user(orig_user)


def on_cancel_sales_invoice(doc, method=None) -> None:
	"""Bypass Purchase Invoice link check and clear IC reference when Sales Invoice cancels."""
	doc.inter_company_invoice_reference = None
	frappe.db.set_value("Sales Invoice", doc.name, "inter_company_invoice_reference", None)
	_add_ignore_linked_doctype(doc, "Purchase Invoice")


def on_cancel_purchase_invoice(doc, method=None) -> None:
	"""Bypass Sales Invoice link check and clear IC reference when Purchase Invoice cancels."""
	doc.inter_company_invoice_reference = None
	frappe.db.set_value("Purchase Invoice", doc.name, "inter_company_invoice_reference", None)
	_add_ignore_linked_doctype(doc, "Sales Invoice")


def before_cancel_payment_entry(doc, method=None) -> None:
	"""Handle Payment Entry cancellation cascade between outlet and JURI."""
	if getattr(doc.flags, "ic_cascade", False):
		return

	if doc.payment_type == "Pay" and doc.party_type == "Supplier":
		juri_pe_name = doc.ic_payment_reference or frappe.db.get_value(
			"Payment Entry",
			{"ic_payment_reference": doc.name, "docstatus": ("<", 2)},
			"name",
		)
		if juri_pe_name and frappe.db.exists("Payment Entry", juri_pe_name):
			juri_doc = frappe.get_doc("Payment Entry", juri_pe_name)
			orig_user = frappe.session.user
			try:
				frappe.set_user("Administrator")
				if juri_doc.docstatus == 0:
					frappe.db.set_value("Payment Entry", doc.name, "ic_payment_reference", None)
					doc.ic_payment_reference = None
					frappe.delete_doc("Payment Entry", juri_pe_name, force=1, ignore_permissions=True)
				elif juri_doc.docstatus == 1:
					juri_doc.flags.ignore_permissions = True
					juri_doc.flags.ic_cascade = True
					juri_doc.cancel()
			finally:
				frappe.set_user(orig_user)

	elif doc.payment_type == "Receive" and doc.party_type == "Customer":
		outlet_pe_name = doc.ic_payment_reference or frappe.db.get_value(
			"Payment Entry",
			{"ic_payment_reference": doc.name, "docstatus": ("<", 2)},
			"name",
		)
		if outlet_pe_name and frappe.db.exists("Payment Entry", outlet_pe_name):
			frappe.db.set_value("Payment Entry", outlet_pe_name, "ic_payment_reference", None)
		doc.ic_payment_reference = None


def on_cancel_payment_entry(doc, method=None) -> None:
	"""Bypass Payment Entry backlink check and ensure references are cleared."""
	_add_ignore_linked_doctype(doc, "Payment Entry")
	if doc.payment_type == "Receive":
		outlet_pe_name = frappe.db.get_value(
			"Payment Entry",
			{"ic_payment_reference": doc.name, "docstatus": ("<", 2)},
			"name",
		)
		if outlet_pe_name:
			frappe.db.set_value("Payment Entry", outlet_pe_name, "ic_payment_reference", None)
