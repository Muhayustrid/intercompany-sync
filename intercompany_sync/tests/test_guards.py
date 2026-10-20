import frappe
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from erpnext.stock.doctype.delivery_note.delivery_note import make_inter_company_purchase_receipt
from frappe.tests import IntegrationTestCase

from intercompany_sync.tests.factories import (
	ICO_ABBR,
	ICS_ABBR,
	INT_CUSTOMER,
	INT_SUPPLIER,
	ITEM,
	OUTLET_CO,
	PRICE_LIST,
	SUPPLIER_CO,
	make_dn,
	make_pr,
	make_si,
	setup_world,
)


class TestGuards(IntegrationTestCase):
	def setUp(self):
		setup_world()

	def test_pr_without_dn_blocked(self):
		pr = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"items": [
					{
						"item_code": ITEM,
						"qty": 1,
						"rate": 1000,
						"warehouse": f"Stores - {ICO_ABBR}",
					}
				],
			}
		)
		self.assertRaises(frappe.ValidationError, pr.insert)

	def test_pr_from_dn_allowed(self):
		dn = make_dn()
		pr = make_pr(dn.name)
		self.assertEqual(pr.docstatus, 1)

	def test_si_without_dn_blocked(self):
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": SUPPLIER_CO,
				"customer": INT_CUSTOMER,
				"currency": "IDR",
				"selling_price_list": PRICE_LIST,
				"items": [
					{
						"item_code": ITEM,
						"qty": 1,
						"rate": 1000,
						"warehouse": f"Stores - {ICS_ABBR}",
					}
				],
			}
		)
		self.assertRaises(frappe.ValidationError, si.insert)

	def test_si_before_pr_blocked(self):
		dn = make_dn()
		self.assertRaises(frappe.ValidationError, make_si, dn.name)
		make_pr(dn.name)
		si = make_si(dn.name)
		self.assertEqual(si.docstatus, 1)

	def test_si_with_draft_pr_blocked(self):
		dn = make_dn()
		draft_pr = make_inter_company_purchase_receipt(dn.name)
		for item in draft_pr.items:
			if not item.warehouse:
				item.warehouse = f"Stores - {ICO_ABBR}"
		draft_pr.insert()
		self.assertEqual(draft_pr.docstatus, 0)
		self.assertRaises(frappe.ValidationError, make_si, dn.name)

	def test_returns_untouched(self):
		dn = make_dn(qty=5)
		pr = make_pr(dn.name)
		si = make_si(dn.name)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(si.docstatus, 1)

		pr_return = make_return_doc("Purchase Receipt", pr.name)
		pr_return.insert()
		pr_return.submit()
		self.assertEqual(pr_return.docstatus, 1)
		self.assertEqual(pr_return.is_return, 1)

		si_return = make_return_doc("Sales Invoice", si.name)
		si_return.insert()
		si_return.submit()
		self.assertEqual(si_return.docstatus, 1)
		self.assertEqual(si_return.is_return, 1)
