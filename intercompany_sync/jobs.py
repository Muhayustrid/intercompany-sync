import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice


def log(
	action: str,
	source_doctype: str,
	source_name: str,
	target_doctype: str | None,
	target_name: str | None,
	error: str | None = None,
	ignored_permissions: bool = False,
) -> None:
	"""Insert an IC Sync Log entry."""
	frappe.get_doc(
		{
			"doctype": "IC Sync Log",
			"action": action,
			"status": "Failed" if error else "Success",
			"source_doctype": source_doctype,
			"source_name": source_name,
			"target_doctype": target_doctype,
			"target_name": target_name,
			"user": frappe.session.user,
			"ignored_permissions": 1 if ignored_permissions else 0,
			"error": error,
		}
	).insert(ignore_permissions=True)


def on_sales_invoice_submit(doc, method=None) -> None:
	"""Enqueue create_purchase_invoice job on internal Sales Invoice submit."""
	if doc.is_return or doc.is_pos:
		return

	is_internal = doc.is_internal_customer or (
		doc.customer and frappe.db.get_value("Customer", doc.customer, "is_internal_customer")
	)
	if not is_internal:
		return

	frappe.enqueue(
		"intercompany_sync.jobs.create_purchase_invoice",
		queue="short",
		enqueue_after_commit=True,
		now=bool(getattr(frappe.flags, "in_test", False) or getattr(frappe, "in_test", False)),
		si_name=doc.name,
	)


def create_purchase_invoice(si_name: str) -> str | None:
	"""Create and submit counterpart Purchase Invoice for an internal Sales Invoice."""
	if frappe.db.get_value("Sales Invoice", si_name, "docstatus") != 1:
		return None

	existing_pi = frappe.db.get_value(
		"Purchase Invoice",
		{"inter_company_invoice_reference": si_name, "docstatus": ("<", 2)},
		"name",
	)
	if existing_pi:
		return existing_pi

	ignored_permissions = False
	source_user = frappe.session.user
	savepoint = "ic_job"
	frappe.db.savepoint(savepoint)

	try:
		try:
			pi = make_inter_company_purchase_invoice(si_name)
		except frappe.PermissionError:
			ignored_permissions = True
			frappe.set_user("Administrator")
			try:
				pi = make_inter_company_purchase_invoice(si_name)
			finally:
				frappe.set_user(source_user)

		si = frappe.db.get_value(
			"Sales Invoice",
			si_name,
			["posting_date", "posting_time"],
			as_dict=True,
		)
		pi.bill_no = si_name
		pi.bill_date = si.posting_date
		pi.posting_date = si.posting_date
		pi.set_posting_time = 1
		pi.posting_time = si.posting_time
		pi.due_date = None

		pi.insert(ignore_permissions=True)
		pi.flags.ignore_permissions = True
		pi.submit()

		log(
			"create_purchase_invoice",
			"Sales Invoice",
			si_name,
			"Purchase Invoice",
			pi.name,
			ignored_permissions=ignored_permissions,
		)
		return pi.name
	except Exception:  # noqa: BLE001 - job boundary: record failure in IC Sync Log
		frappe.db.rollback(save_point=savepoint)
		frappe.set_user(source_user)
		log(
			"create_purchase_invoice",
			"Sales Invoice",
			si_name,
			None,
			None,
			error=frappe.get_traceback(),
			ignored_permissions=ignored_permissions,
		)
		return None


def on_payment_entry_submit(doc, method=None) -> None:
	"""Enqueue create_juri_payment job on outlet Payment Entry submit."""
	if doc.payment_type != "Pay" or doc.party_type != "Supplier":
		return

	is_internal = doc.party and frappe.db.get_value("Supplier", doc.party, "is_internal_supplier")
	if not is_internal:
		return

	pi_names = [
		ref.reference_name
		for ref in (doc.references or [])
		if ref.reference_doctype == "Purchase Invoice" and ref.reference_name
	]
	if not pi_names:
		return

	has_ic = False
	for pi_name in pi_names:
		if frappe.db.get_value("Purchase Invoice", pi_name, "inter_company_invoice_reference"):
			has_ic = True
			break
	if not has_ic:
		return

	frappe.enqueue(
		"intercompany_sync.jobs.create_juri_payment",
		queue="short",
		enqueue_after_commit=True,
		now=bool(getattr(frappe.flags, "in_test", False) or getattr(frappe, "in_test", False)),
		pe_name=doc.name,
	)


def create_juri_payment(pe_name: str) -> str | None:
	"""Create draft counterpart Payment Entry in JURI for an outlet Payment Entry."""
	if frappe.db.get_value("Payment Entry", pe_name, "docstatus") != 1:
		return None

	existing_pe = frappe.db.get_value(
		"Payment Entry",
		{"ic_payment_reference": pe_name, "docstatus": ("<", 2)},
		"name",
	)
	if existing_pe:
		return existing_pe

	pe = frappe.get_doc("Payment Entry", pe_name)
	if pe.ic_payment_reference and frappe.db.exists(
		"Payment Entry", {"name": pe.ic_payment_reference, "docstatus": ("<", 2)}
	):
		return pe.ic_payment_reference

	if pe.payment_type != "Pay" or pe.party_type != "Supplier":
		return None

	si_allocations: dict[str, float] = {}
	for ref in pe.get("references", []):
		if ref.reference_doctype != "Purchase Invoice" or not ref.reference_name:
			continue
		si_name = frappe.db.get_value(
			"Purchase Invoice",
			ref.reference_name,
			"inter_company_invoice_reference",
		)
		if not si_name:
			continue
		si_allocations[si_name] = si_allocations.get(si_name, 0.0) + float(ref.allocated_amount or 0.0)

	if not si_allocations:
		return None

	first_si = next(iter(si_allocations.keys()))
	ignored_permissions = False
	source_user = frappe.session.user
	savepoint = "ic_job"
	frappe.db.savepoint(savepoint)

	try:
		try:
			juri_pe = get_payment_entry("Sales Invoice", first_si)
		except frappe.PermissionError:
			ignored_permissions = True
			frappe.set_user("Administrator")
			juri_pe = get_payment_entry("Sales Invoice", first_si)

		juri_pe.references = []
		for si_name, alloc_amt in si_allocations.items():
			si_data = frappe.db.get_value(
				"Sales Invoice",
				si_name,
				["due_date", "grand_total", "outstanding_amount"],
				as_dict=True,
			)
			juri_pe.append(
				"references",
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si_name,
					"due_date": si_data.due_date,
					"total_amount": si_data.grand_total,
					"outstanding_amount": si_data.outstanding_amount,
					"allocated_amount": alloc_amt,
				},
			)

		total_allocated = sum(si_allocations.values())
		juri_pe.paid_amount = total_allocated
		juri_pe.received_amount = total_allocated
		juri_pe.reference_no = pe.reference_no
		juri_pe.reference_date = pe.reference_date
		juri_pe.posting_date = pe.posting_date
		juri_pe.ic_payment_reference = pe_name
		juri_pe.flags.ignore_permissions = True

		if ignored_permissions:
			juri_pe.insert(ignore_permissions=True)
			frappe.db.set_value("Payment Entry", juri_pe.name, "owner", source_user)
			juri_pe.owner = source_user
			frappe.set_user(source_user)
		else:
			try:
				juri_pe.insert(ignore_permissions=True)
			except frappe.PermissionError:
				ignored_permissions = True
				frappe.set_user("Administrator")
				juri_pe.insert(ignore_permissions=True)
				frappe.db.set_value("Payment Entry", juri_pe.name, "owner", source_user)
				juri_pe.owner = source_user
				frappe.set_user(source_user)

		frappe.db.set_value("Payment Entry", pe_name, "ic_payment_reference", juri_pe.name)

		log(
			"create_juri_payment",
			"Payment Entry",
			pe_name,
			"Payment Entry",
			juri_pe.name,
			ignored_permissions=ignored_permissions,
		)
		return juri_pe.name
	except Exception:  # noqa: BLE001 - job boundary: record failure in IC Sync Log
		frappe.db.rollback(save_point=savepoint)
		frappe.set_user(source_user)
		log(
			"create_juri_payment",
			"Payment Entry",
			pe_name,
			None,
			None,
			error=frappe.get_traceback(),
			ignored_permissions=ignored_permissions,
		)
		return None
