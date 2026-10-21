import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from intercompany_sync.tests.factories import (
	ICO_ABBR,
	INT_SUPPLIER,
	ITEM,
	OUTLET_CO,
	get_pi,
	make_dn,
	make_pr,
	make_si,
	pay_pis,
	setup_world,
)


def _make_extra_pi(si_name: str, qty: float = 1, rate: float = 1000):
	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": OUTLET_CO,
			"supplier": INT_SUPPLIER,
			"currency": "IDR",
			"inter_company_invoice_reference": si_name,
			"bill_no": f"{si_name}-EXTRA",
			"bill_date": today(),
			"posting_date": today(),
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"rate": rate,
					"warehouse": f"Stores - {ICO_ABBR}",
				}
			],
		}
	)
	pi.insert(ignore_permissions=True)
	pi.submit()
	return pi


class TestCancel(IntegrationTestCase):
	def setUp(self):
		setup_world()
		if frappe.db.exists("DocType", "IC Sync Settings"):
			frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
			frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

	def tearDown(self):
		from intercompany_sync.tests.factories import INT_CUSTOMER, OUTLET_CO

		if frappe.db.exists("Customer", INT_CUSTOMER):
			frappe.db.set_value(
				"Customer",
				INT_CUSTOMER,
				{"is_internal_customer": 1, "represents_company": OUTLET_CO},
			)
		if frappe.db.exists("DocType", "IC Sync Settings"):
			frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
			frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

	def test_si_cancel_cascades_pi(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)
		self.assertEqual(pi.docstatus, 1)

		si.reload()
		si.cancel()
		pi.reload()
		self.assertEqual(pi.docstatus, 2)

	def test_si_cancel_blocked_when_paid(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		pay_pis({pi.name: 1000})
		pi.reload()
		self.assertLess(pi.outstanding_amount, pi.grand_total)

		si.reload()
		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		self.assertEqual(si.docstatus, 1)

	def test_pi_manual_cancel_blocked(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		with self.assertRaises(frappe.ValidationError) as cm:
			pi.cancel()
		self.assertIn("Batalkan lewat Sales Invoice", str(cm.exception))

		pi.reload()
		self.assertEqual(pi.docstatus, 1)

	def test_outlet_pe_cancel_deletes_draft(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		pe = pay_pis({pi.name: 2000})
		pe.reload()

		juri_pe_name = pe.ic_payment_reference
		self.assertTrue(bool(juri_pe_name))
		juri_pe = frappe.get_doc("Payment Entry", juri_pe_name)
		self.assertEqual(juri_pe.docstatus, 0)

		pe.cancel()
		self.assertFalse(frappe.db.exists("Payment Entry", juri_pe_name))

	def test_outlet_pe_cancel_cancels_submitted(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		pe = pay_pis({pi.name: 2000})
		pe.reload()

		juri_pe = frappe.get_doc("Payment Entry", pe.ic_payment_reference)
		juri_pe.submit()
		self.assertEqual(juri_pe.docstatus, 1)

		pe.cancel()
		juri_pe.reload()
		self.assertEqual(juri_pe.docstatus, 2)

	def test_juri_pe_cancel_clears_link(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		pe = pay_pis({pi.name: 2000})
		pe.reload()

		juri_pe = frappe.get_doc("Payment Entry", pe.ic_payment_reference)
		juri_pe.submit()
		juri_pe.cancel()
		self.assertEqual(juri_pe.docstatus, 2)

		pe.reload()
		self.assertFalse(bool(pe.ic_payment_reference))
		self.assertEqual(pe.docstatus, 1)

	def test_dn_cancel_blocked_with_pr(self):
		dn = make_dn()
		make_pr(dn.name)

		with self.assertRaises(frappe.ValidationError) as cm:
			dn.cancel()
		self.assertIn("sudah diterima lewat Purchase Receipt", str(cm.exception))

		dn.reload()
		self.assertEqual(dn.docstatus, 1)

	def test_amend_no_duplicate(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		si.reload()
		si.cancel()
		pi.reload()
		self.assertEqual(pi.docstatus, 2)

		amended_si = frappe.copy_doc(si)
		amended_si.docstatus = 0
		amended_si.amended_from = si.name
		amended_si.save()
		amended_si.submit()

		new_pi = get_pi(amended_si.name)
		self.assertIsNotNone(new_pi)
		self.assertEqual(new_pi.docstatus, 1)

		old_pi = get_pi(si.name)
		self.assertIsNone(old_pi)

	def test_pr_cancel_blocked_when_pi_submitted(self):
		dn = make_dn()
		pr = make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)
		self.assertEqual(pi.docstatus, 1)

		pr.reload()
		with self.assertRaises((frappe.ValidationError, frappe.LinkExistsError)):
			pr.cancel()

	def test_si_cancel_blocked_when_paid_even_if_internal_flags_cleared(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		pay_pis({pi.name: 1000})
		pi.reload()
		self.assertLess(pi.outstanding_amount, pi.grand_total)

		frappe.db.set_value("Sales Invoice", si.name, "is_internal_customer", 0)
		frappe.db.set_value("Customer", si.customer, "is_internal_customer", 0)
		si.reload()

		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		self.assertEqual(si.docstatus, 1)

	def test_si_cancel_cascades_pi_even_if_internal_flags_cleared(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		frappe.db.set_value("Sales Invoice", si.name, "is_internal_customer", 0)
		frappe.db.set_value("Customer", si.customer, "is_internal_customer", 0)
		si.reload()

		si.cancel()
		pi.reload()
		self.assertEqual(pi.docstatus, 2)

	def test_si_cancel_cancels_all_referencing_pis(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)
		pi2 = _make_extra_pi(si.name)

		si.reload()
		si.cancel()

		pi1.reload()
		pi2.reload()
		self.assertEqual(pi1.docstatus, 2)
		self.assertEqual(pi2.docstatus, 2)

	def test_si_cancel_blocked_if_any_referencing_pi_is_paid(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)
		pi2 = _make_extra_pi(si.name)

		pay_pis({pi2.name: 1000})
		pi2.reload()
		self.assertLess(pi2.outstanding_amount, pi2.grand_total)

		si.reload()
		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		pi1.reload()
		pi2.reload()
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(pi1.docstatus, 1)
		self.assertEqual(pi2.docstatus, 1)

	def test_si_cancel_blocked_if_first_referencing_pi_is_paid(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)
		pi2 = _make_extra_pi(si.name)

		pay_pis({pi1.name: 1000})
		pi1.reload()
		self.assertLess(pi1.outstanding_amount, pi1.grand_total)

		si.reload()
		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		pi1.reload()
		pi2.reload()
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(pi1.docstatus, 1)
		self.assertEqual(pi2.docstatus, 1)

	def test_cascade_cancel_works_when_toggles_enabled(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		pe = pay_pis({pi.name: 2000})

		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 1)
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 1)

		pe.reload()
		juri_pe_name = pe.ic_payment_reference
		pe.cancel()
		self.assertFalse(frappe.db.exists("Payment Entry", juri_pe_name))

		si.reload()
		si.cancel()
		pi.reload()
		self.assertEqual(pi.docstatus, 2)

	def test_si_cancel_blocked_when_paid_even_if_is_return(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		pay_pis({pi.name: 1000})
		pi.reload()
		self.assertLess(pi.outstanding_amount, pi.grand_total)

		frappe.db.set_value("Sales Invoice", si.name, "is_return", 1)
		si.reload()

		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		self.assertEqual(si.docstatus, 1)

	def test_si_cancel_deletes_draft_referencing_pi(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)

		draft_pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"inter_company_invoice_reference": si.name,
				"bill_no": f"{si.name}-DRAFT",
				"bill_date": today(),
				"posting_date": today(),
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
		draft_pi.insert(ignore_permissions=True)
		self.assertEqual(draft_pi.docstatus, 0)

		si.reload()
		si.cancel()

		pi1.reload()
		self.assertEqual(pi1.docstatus, 2)
		self.assertFalse(frappe.db.exists("Purchase Invoice", draft_pi.name))

	def test_si_cancel_rollback_on_later_pi_failure(self):
		from unittest.mock import patch
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice

		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)
		pi2 = _make_extra_pi(si.name)

		sorted_names = sorted([pi1.name, pi2.name])
		first_pi_name, second_pi_name = sorted_names[0], sorted_names[1]

		orig_cancel = PurchaseInvoice.cancel

		def fail_second_pi(doc_self, *args, **kwargs):
			if doc_self.name == second_pi_name:
				frappe.throw("Simulated PI cancel failure")
			return orig_cancel(doc_self, *args, **kwargs)

		with patch.object(PurchaseInvoice, "cancel", fail_second_pi):
			si.reload()
			with self.assertRaises(frappe.ValidationError):
				si.cancel()

		si.reload()
		self.assertEqual(si.docstatus, 1)
		first_pi = frappe.get_doc("Purchase Invoice", first_pi_name)
		self.assertEqual(first_pi.docstatus, 1)
		second_pi = frappe.get_doc("Purchase Invoice", second_pi_name)
		self.assertEqual(second_pi.docstatus, 1)

	def test_si_cancel_blocked_when_draft_pi_has_linking_doc(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		self.assertIsNotNone(pi1)

		draft_pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"inter_company_invoice_reference": si.name,
				"bill_no": f"{si.name}-DRAFT-LINKED",
				"bill_date": today(),
				"posting_date": today(),
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
		draft_pi.insert(ignore_permissions=True)
		self.assertEqual(draft_pi.docstatus, 0)

		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"company": OUTLET_CO,
				"party_type": "Supplier",
				"party": INT_SUPPLIER,
				"paid_amount": 1000,
				"received_amount": 1000,
				"paid_from": frappe.get_value("Company", OUTLET_CO, "default_cash_account"),
				"paid_to": frappe.get_value("Company", OUTLET_CO, "default_payable_account"),
				"paid_from_account_currency": "IDR",
				"paid_to_account_currency": "IDR",
				"source_exchange_rate": 1.0,
				"target_exchange_rate": 1.0,
				"base_paid_amount": 1000,
				"base_received_amount": 1000,
				"reference_no": "DRAFT-PI-LINK",
				"reference_date": today(),
				"references": [
					{
						"reference_doctype": "Purchase Invoice",
						"reference_name": draft_pi.name,
						"total_amount": 1000,
						"outstanding_amount": 1000,
						"allocated_amount": 1000,
					}
				],
			}
		)
		pe.flags.ignore_validate = True
		pe.insert(ignore_permissions=True, ignore_mandatory=True)
		self.assertEqual(pe.docstatus, 0)

		si.reload()
		with self.assertRaises((frappe.LinkExistsError, frappe.ValidationError)):
			si.cancel()

		self.assertTrue(frappe.db.exists("Purchase Invoice", draft_pi.name))
		si.reload()
		self.assertEqual(si.docstatus, 1)

	def test_si_cancel_blocked_when_return_pi_is_partially_paid(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		return_pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"is_return": 1,
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"inter_company_invoice_reference": si.name,
				"bill_no": f"{si.name}-RET-PARTIAL",
				"bill_date": today(),
				"posting_date": today(),
				"items": [
					{
						"item_code": ITEM,
						"qty": -1,
						"rate": 1000,
						"warehouse": f"Stores - {ICO_ABBR}",
					}
				],
			}
		)
		return_pi.insert(ignore_permissions=True)
		return_pi.submit()
		self.assertEqual(return_pi.docstatus, 1)
		self.assertLess(return_pi.grand_total, 0)

		frappe.db.set_value("Purchase Invoice", return_pi.name, "outstanding_amount", -600)
		return_pi.reload()
		self.assertEqual(return_pi.outstanding_amount, -600)

		si.reload()
		with self.assertRaises(frappe.ValidationError) as cm:
			si.cancel()
		self.assertIn("sudah dibayar", str(cm.exception))

		si.reload()
		self.assertEqual(si.docstatus, 1)

	def test_si_cancel_deletes_draft_pi_when_si_points_at_it(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi1 = get_pi(si.name)
		if pi1:
			pi1.flags.ignore_permissions = True
			pi1.flags.ic_cascade = True
			pi1.cancel()

		draft_pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"inter_company_invoice_reference": si.name,
				"bill_no": f"{si.name}-DRAFT-SELF",
				"bill_date": today(),
				"posting_date": today(),
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
		draft_pi.insert(ignore_permissions=True)
		self.assertEqual(draft_pi.docstatus, 0)

		frappe.db.set_value("Sales Invoice", si.name, "inter_company_invoice_reference", draft_pi.name)
		si.reload()
		self.assertEqual(si.inter_company_invoice_reference, draft_pi.name)

		si.cancel()

		self.assertEqual(si.docstatus, 2)
		self.assertFalse(frappe.db.exists("Purchase Invoice", draft_pi.name))
