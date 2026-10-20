import frappe
from frappe.tests import IntegrationTestCase

from intercompany_sync.tests.factories import (
	get_pi,
	make_dn,
	make_pr,
	make_si,
	pay_pis,
	setup_world,
)


class TestCancel(IntegrationTestCase):
	def setUp(self):
		setup_world()

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
