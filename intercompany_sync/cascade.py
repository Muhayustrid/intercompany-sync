import frappe
from frappe import _
from frappe.utils import flt


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
	"""Cascade cancellation to counterpart Purchase Invoices or block if already paid."""
	pi_names = set(
		frappe.get_all(
			"Purchase Invoice",
			filters={"inter_company_invoice_reference": doc.name, "docstatus": ("<", 2)},
			pluck="name",
		)
	)
	if doc.inter_company_invoice_reference and frappe.db.exists(
		"Purchase Invoice",
		{"name": doc.inter_company_invoice_reference, "docstatus": ("<", 2)},
	):
		pi_names.add(doc.inter_company_invoice_reference)

	if not pi_names:
		return

	pi_docs = [frappe.get_doc("Purchase Invoice", name) for name in sorted(pi_names)]
	for pi_doc in pi_docs:
		# Outstanding starts at rounded_total (if rounding is on); abs() keeps returns (negative totals) right.
		invoice_total = flt(pi_doc.rounded_total) or flt(pi_doc.grand_total)
		if pi_doc.docstatus == 1 and abs(flt(pi_doc.outstanding_amount) - invoice_total) > 0.01:
			frappe.throw(
				_("Purchase Invoice {0} sudah dibayar. Batalkan pembayarannya dulu.").format(pi_doc.name),
				title=_("Purchase Invoice Sudah Dibayar"),
			)

	orig_user = frappe.session.user
	# All-or-nothing: a failure on a later PI must not leave earlier ones cancelled.
	frappe.db.savepoint("ic_si_cancel_cascade")
	try:
		frappe.set_user("Administrator")
		for pi_doc in pi_docs:
			if pi_doc.docstatus == 1:
				pi_doc.flags.ignore_permissions = True
				pi_doc.flags.ic_cascade = True
				pi_doc.cancel()
			elif pi_doc.docstatus == 0:
				# No force: other docs linking the draft must block; unlink this SI first so it does not.
				if doc.inter_company_invoice_reference == pi_doc.name:
					frappe.db.set_value("Sales Invoice", doc.name, "inter_company_invoice_reference", None)
				frappe.delete_doc("Purchase Invoice", pi_doc.name, ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="ic_si_cancel_cascade")
		raise
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
