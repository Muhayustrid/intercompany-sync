# Copyright (c) 2026, Rotiri Project and contributors
# License: MIT

import frappe
from frappe import _
from frappe.model.document import Document


class ICSyncLog(Document):
	def _validate_links(self):
		pass

	@frappe.whitelist()
	def retry(self) -> str | None:
		return retry(self.name)


@frappe.whitelist()
def retry(name: str) -> str | None:
	doc = frappe.get_doc("IC Sync Log", name)
	doc.check_permission("write")

	source_status = (
		frappe.db.get_value(doc.source_doctype, doc.source_name, "docstatus")
		if doc.source_doctype and doc.source_name
		else None
	)
	if source_status != 1:
		frappe.throw(
			_("Dokumen sumber {0} {1} tidak aktif (docstatus != 1).").format(
				doc.source_doctype, doc.source_name
			),
			title=_("Dokumen Sumber Tidak Aktif"),
		)

	target_name = None
	if doc.action == "create_purchase_invoice":
		from intercompany_sync.jobs import create_purchase_invoice

		target_name = create_purchase_invoice(doc.source_name)
		if not target_name:
			target_name = frappe.db.get_value(
				"Purchase Invoice",
				{"inter_company_invoice_reference": doc.source_name, "docstatus": ("<", 2)},
				"name",
			)
		if target_name:
			doc.db_set(
				{
					"status": "Success",
					"target_doctype": "Purchase Invoice",
					"target_name": target_name,
					"error": None,
				}
			)

	elif doc.action == "create_juri_payment":
		from intercompany_sync.jobs import create_juri_payment

		target_name = create_juri_payment(doc.source_name)
		if not target_name:
			target_name = frappe.db.get_value(
				"Payment Entry",
				{"ic_payment_reference": doc.source_name, "docstatus": ("<", 2)},
				"name",
			)
			if not target_name:
				ref = frappe.db.get_value(
					"Payment Entry", doc.source_name, "ic_payment_reference"
				)
				if ref and frappe.db.exists(
					"Payment Entry", {"name": ref, "docstatus": ("<", 2)}
				):
					target_name = ref
		if target_name:
			doc.db_set(
				{
					"status": "Success",
					"target_doctype": "Payment Entry",
					"target_name": target_name,
					"error": None,
				}
			)

	return target_name

