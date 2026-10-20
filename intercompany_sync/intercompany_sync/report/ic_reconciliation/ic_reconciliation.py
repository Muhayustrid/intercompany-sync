# Copyright (c) 2026, Rotiri Project and contributors
# License: MIT

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Issue"),
			"fieldname": "issue",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("Source DocType"),
			"fieldname": "source_doctype",
			"fieldtype": "Link",
			"options": "DocType",
			"width": 140,
		},
		{
			"label": _("Source Name"),
			"fieldname": "source_name",
			"fieldtype": "Dynamic Link",
			"options": "source_doctype",
			"width": 180,
		},
		{
			"label": _("Counterpart"),
			"fieldname": "counterpart",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("Difference"),
			"fieldname": "difference",
			"fieldtype": "Currency",
			"width": 120,
		},
	]


def get_data(filters):
	data = []
	target_company = filters.get("company")

	# 1. SI tanpa PI & Selisih nominal
	sales_invoices = frappe.db.sql(
		"""
		SELECT
			si.name,
			si.company,
			si.customer,
			si.grand_total,
			si.is_internal_customer,
			si.inter_company_invoice_reference,
			c.represents_company
		FROM `tabSales Invoice` si
		INNER JOIN `tabCustomer` c ON c.name = si.customer
		WHERE si.docstatus = 1
		  AND si.is_return = 0
		  AND (c.is_internal_customer = 1 OR si.is_internal_customer = 1)
		ORDER BY si.creation ASC
		""",
		as_dict=True,
	)

	for si in sales_invoices:
		if target_company and target_company not in (si.company, si.represents_company):
			continue

		active_pis = frappe.get_all(
			"Purchase Invoice",
			filters={"inter_company_invoice_reference": si.name, "docstatus": 1},
			fields=["name", "grand_total"],
		)
		if not active_pis and si.get("inter_company_invoice_reference"):
			active_pis = frappe.get_all(
				"Purchase Invoice",
				filters={"name": si.inter_company_invoice_reference, "docstatus": 1},
				fields=["name", "grand_total"],
			)

		if not active_pis:
			data.append(
				{
					"issue": "SI tanpa PI",
					"source_doctype": "Sales Invoice",
					"source_name": si.name,
					"counterpart": None,
					"difference": 0.0,
				}
			)
		else:
			for pi in active_pis:
				diff = flt(si.grand_total) - flt(pi.grand_total)
				if abs(diff) > 0.001:
					data.append(
						{
							"issue": "Selisih nominal",
							"source_doctype": "Sales Invoice",
							"source_name": si.name,
							"counterpart": pi.name,
							"difference": diff,
						}
					)

	# 2. PE outlet tanpa PE JURI
	payment_entries = frappe.db.sql(
		"""
		SELECT
			pe.name,
			pe.company,
			pe.party,
			pe.ic_payment_reference,
			s.represents_company
		FROM `tabPayment Entry` pe
		INNER JOIN `tabSupplier` s ON s.name = pe.party
		WHERE pe.docstatus = 1
		  AND pe.payment_type = 'Pay'
		  AND pe.party_type = 'Supplier'
		  AND s.is_internal_supplier = 1
		ORDER BY pe.creation ASC
		""",
		as_dict=True,
	)

	candidate_pes = []
	for pe in payment_entries:
		if target_company and target_company not in (pe.company, pe.represents_company):
			continue

		if pe.ic_payment_reference:
			ref_status = frappe.db.get_value("Payment Entry", pe.ic_payment_reference, "docstatus")
			if ref_status is not None and ref_status < 2:
				continue

		candidate_pes.append(pe)

	if candidate_pes:
		pe_names = [p.name for p in candidate_pes]
		ic_pe_parents = set(
			frappe.db.sql_list(
				"""
				SELECT DISTINCT per.parent
				FROM `tabPayment Entry Reference` per
				INNER JOIN `tabPurchase Invoice` pi ON pi.name = per.reference_name
				WHERE per.parent IN %(pe_names)s
				  AND per.reference_doctype = 'Purchase Invoice'
				  AND pi.inter_company_invoice_reference IS NOT NULL
				  AND pi.inter_company_invoice_reference != ''
				""",
				{"pe_names": pe_names},
			)
		)
		for pe in candidate_pes:
			if pe.name in ic_pe_parents:
				data.append(
					{
						"issue": "PE outlet tanpa PE JURI",
						"source_doctype": "Payment Entry",
						"source_name": pe.name,
						"counterpart": None,
						"difference": 0.0,
					}
				)

	return data
