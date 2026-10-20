from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from intercompany_sync.tests.factories import (
	ICO_ABBR,
	INT_SUPPLIER,
	ITEM,
	OUTLET_CO,
	OUTLET_USER,
	SUPPLIER_CO,
	get_pi,
	make_dn,
	make_pr,
	make_si,
	pay_pis,
	setup_world,
)


class TestJuriPaymentJob(IntegrationTestCase):
	def setUp(self):
		setup_world()

	def test_full_payment_creates_draft(self):
		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		outlet_pe = pay_pis({pi.name: 2000})
		outlet_pe.reload()

		self.assertTrue(bool(outlet_pe.ic_payment_reference))
		juri_pe = frappe.get_doc("Payment Entry", outlet_pe.ic_payment_reference)

		self.assertEqual(juri_pe.docstatus, 0)
		self.assertEqual(juri_pe.company, SUPPLIER_CO)
		self.assertEqual(juri_pe.payment_type, "Receive")
		self.assertEqual(juri_pe.paid_to, "Bank - ICS")
		si.reload()
		self.assertEqual(juri_pe.paid_from, si.debit_to)
		self.assertEqual(len(juri_pe.references), 1)

		ref = juri_pe.references[0]
		self.assertEqual(ref.reference_doctype, "Sales Invoice")
		self.assertEqual(ref.reference_name, si.name)
		self.assertEqual(ref.allocated_amount, 2000)

		self.assertEqual(juri_pe.reference_no, "ICTC-REF")
		self.assertEqual(juri_pe.ic_payment_reference, outlet_pe.name)
		self.assertEqual(outlet_pe.ic_payment_reference, juri_pe.name)

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_juri_payment", "source_name": outlet_pe.name},
			fields=["status", "target_name"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[0].target_name, juri_pe.name)

	def test_partial_multi_invoice(self):
		dn1 = make_dn(qty=2, rate=1000)
		make_pr(dn1.name)
		si1 = make_si(dn1.name)
		pi1 = get_pi(si1.name)

		dn2 = make_dn(qty=3, rate=1000)
		make_pr(dn2.name)
		si2 = make_si(dn2.name)
		pi2 = get_pi(si2.name)

		outlet_pe = pay_pis({pi1.name: 2000, pi2.name: 1000})
		outlet_pe.reload()

		juri_pe = frappe.get_doc("Payment Entry", outlet_pe.ic_payment_reference)
		self.assertEqual(juri_pe.docstatus, 0)
		self.assertEqual(juri_pe.paid_amount, 3000)
		self.assertEqual(juri_pe.received_amount, 3000)

		refs = {r.reference_name: r.allocated_amount for r in juri_pe.references}
		self.assertEqual(refs, {si1.name: 2000, si2.name: 1000})

		juri_pe.submit()
		self.assertEqual(juri_pe.docstatus, 1)

		si1.reload()
		si2.reload()
		self.assertEqual(si1.outstanding_amount, 0)
		self.assertEqual(si2.outstanding_amount, 2000)

	def test_idempotent(self):
		from intercompany_sync.jobs import create_juri_payment

		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		outlet_pe = pay_pis({pi.name: 2000})

		res1 = create_juri_payment(outlet_pe.name)
		res2 = create_juri_payment(outlet_pe.name)

		active_pes = frappe.get_all(
			"Payment Entry",
			filters={"ic_payment_reference": outlet_pe.name, "docstatus": ("<", 2)},
		)
		self.assertEqual(len(active_pes), 1)
		self.assertEqual(res1, active_pes[0].name)
		self.assertEqual(res2, active_pes[0].name)

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_juri_payment", "source_name": outlet_pe.name},
		)
		self.assertEqual(len(logs), 1)

	def test_non_ic_reference_ignored(self):
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": OUTLET_CO,
				"supplier": INT_SUPPLIER,
				"currency": "IDR",
				"items": [
					{
						"item_code": ITEM,
						"qty": 1,
						"rate": 1000,
						"warehouse": f"Stores - {ICO_ABBR}",
						"expense_account": f"Stock In Hand - {ICO_ABBR}",
					}
				],
			}
		)
		pi.insert()
		pi.submit()

		outlet_pe = pay_pis({pi.name: 1000})
		outlet_pe.reload()

		self.assertFalse(bool(outlet_pe.ic_payment_reference))
		self.assertFalse(
			frappe.db.exists(
				"Payment Entry",
				{"ic_payment_reference": outlet_pe.name, "docstatus": ("<", 2)},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"IC Sync Log",
				{"source_name": outlet_pe.name, "action": "create_juri_payment"},
			)
		)

	def test_receive_ignored(self):
		payable = frappe.db.get_value(
			"Account",
			{"company": OUTLET_CO, "account_type": "Payable"},
		)
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party_type": "Supplier",
				"party": INT_SUPPLIER,
				"company": OUTLET_CO,
				"paid_amount": 500,
				"received_amount": 500,
				"paid_from": payable,
				"paid_to": f"Bank - {ICO_ABBR}",
				"reference_no": "REF-REC",
				"reference_date": today(),
			}
		)
		pe.insert()
		pe.submit()

		pe.reload()
		self.assertFalse(bool(pe.ic_payment_reference))
		self.assertFalse(
			frappe.db.exists(
				"Payment Entry",
				{"ic_payment_reference": pe.name, "docstatus": ("<", 2)},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"IC Sync Log",
				{"source_name": pe.name, "action": "create_juri_payment"},
			)
		)

	def test_runs_as_source_user(self):
		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		outlet_pe = pay_pis({pi.name: 2000}, user=OUTLET_USER)
		outlet_pe.reload()

		self.assertTrue(bool(outlet_pe.ic_payment_reference))
		juri_pe = frappe.get_doc("Payment Entry", outlet_pe.ic_payment_reference)
		self.assertEqual(juri_pe.owner, OUTLET_USER)
		self.assertEqual(juri_pe.docstatus, 0)

		log = frappe.get_doc(
			"IC Sync Log",
			{"source_name": outlet_pe.name, "action": "create_juri_payment"},
		)
		self.assertEqual(log.status, "Success")
		self.assertEqual(log.ignored_permissions, 1)
		self.assertEqual(log.user, OUTLET_USER)

	def test_failure_logged(self):
		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		with patch(
			"intercompany_sync.jobs.get_payment_entry",
			side_effect=frappe.ValidationError("pe boom"),
		):
			outlet_pe = pay_pis({pi.name: 2000})

		outlet_pe.reload()
		self.assertEqual(outlet_pe.docstatus, 1)
		self.assertFalse(bool(outlet_pe.ic_payment_reference))

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_juri_payment", "source_name": outlet_pe.name},
			fields=["status", "error"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Failed")
		self.assertIn("pe boom", logs[0].error or "")

	def test_stale_source_returns_none_no_log(self):
		from intercompany_sync.jobs import create_juri_payment

		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		with patch("intercompany_sync.jobs.create_juri_payment"):
			outlet_pe = pay_pis({pi.name: 2000})

		outlet_pe.reload()
		outlet_pe.cancel()
		self.assertEqual(outlet_pe.docstatus, 2)

		log_count_before = frappe.db.count("IC Sync Log")
		result = create_juri_payment(outlet_pe.name)
		self.assertIsNone(result)
		self.assertEqual(frappe.db.count("IC Sync Log"), log_count_before)
