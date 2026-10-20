from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from frappe.tests import IntegrationTestCase

from intercompany_sync.tests.factories import (
	OUTLET_CO,
	OUTLET_USER,
	get_pi,
	make_dn,
	make_pr,
	make_si,
	setup_world,
)


class TestPurchaseInvoiceJob(IntegrationTestCase):
	def setUp(self):
		setup_world()

	def test_pi_created_and_submitted(self):
		dn = make_dn()
		pr = make_pr(dn.name)
		si = make_si(dn.name)

		pi = get_pi(si.name)
		self.assertIsNotNone(pi)
		self.assertEqual(pi.docstatus, 1)
		self.assertEqual(pi.grand_total, si.grand_total)
		self.assertEqual(pi.bill_no, si.name)
		self.assertEqual(str(pi.posting_date), str(si.posting_date))
		self.assertEqual(pi.company, OUTLET_CO)

		pr.reload()
		self.assertEqual(pr.status, "Completed")

		si.reload()
		self.assertEqual(si.inter_company_invoice_reference, pi.name)

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_purchase_invoice", "source_name": si.name},
			fields=["status", "target_name"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[0].target_name, pi.name)

	def test_idempotent(self):
		from intercompany_sync.jobs import create_purchase_invoice

		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)

		res1 = create_purchase_invoice(si.name)
		res2 = create_purchase_invoice(si.name)

		active_pis = frappe.get_all(
			"Purchase Invoice",
			filters={"inter_company_invoice_reference": si.name, "docstatus": ("<", 2)},
		)
		self.assertEqual(len(active_pis), 1)
		self.assertEqual(res1, active_pis[0].name)
		self.assertEqual(res2, active_pis[0].name)

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_purchase_invoice", "source_name": si.name},
		)
		self.assertEqual(len(logs), 1)

	def test_existing_draft_reused(self):
		from intercompany_sync.jobs import create_purchase_invoice

		dn = make_dn()
		make_pr(dn.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si = make_si(dn.name)

		draft_pi = make_inter_company_purchase_invoice(si.name)
		draft_pi.insert()
		self.assertEqual(draft_pi.docstatus, 0)

		returned_name = create_purchase_invoice(si.name)
		self.assertEqual(returned_name, draft_pi.name)

		active_pis = frappe.get_all(
			"Purchase Invoice",
			filters={"inter_company_invoice_reference": si.name, "docstatus": ("<", 2)},
		)
		self.assertEqual(len(active_pis), 1)

	def test_failure_logged(self):
		dn = make_dn()
		make_pr(dn.name)
		with patch(
			"intercompany_sync.jobs.make_inter_company_purchase_invoice",
			side_effect=frappe.ValidationError("boom"),
		):
			si = make_si(dn.name)

		self.assertEqual(si.docstatus, 1)
		self.assertIsNone(get_pi(si.name))

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_purchase_invoice", "source_name": si.name},
			fields=["status", "error"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Failed")
		self.assertIn("boom", logs[0].error or "")

	def test_runs_as_source_user(self):
		from intercompany_sync.jobs import create_purchase_invoice

		dn = make_dn()
		make_pr(dn.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si = make_si(dn.name)

		orig_user = frappe.session.user
		try:
			frappe.set_user(OUTLET_USER)
			pi_name = create_purchase_invoice(si.name)
		finally:
			frappe.set_user(orig_user)

		pi = frappe.get_doc("Purchase Invoice", pi_name)
		self.assertEqual(pi.owner, OUTLET_USER)
		self.assertEqual(pi.docstatus, 1)

		log = frappe.get_doc("IC Sync Log", {"source_name": si.name, "action": "create_purchase_invoice"})
		self.assertEqual(log.status, "Success")
		self.assertEqual(log.ignored_permissions, 1)
		self.assertEqual(log.user, OUTLET_USER)

	def test_credit_note_no_pi(self):
		dn = make_dn(qty=5)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		pi_count_before = frappe.db.count("Purchase Invoice")

		si_return = make_return_doc("Sales Invoice", si.name)
		si_return.insert()
		si_return.submit()
		self.assertEqual(si_return.docstatus, 1)
		self.assertEqual(si_return.is_return, 1)

		# No new Purchase Invoice created from credit note
		self.assertEqual(frappe.db.count("Purchase Invoice"), pi_count_before)
		self.assertEqual(frappe.db.count("Purchase Invoice", {"is_return": 1}), 0)
		self.assertFalse(frappe.db.exists("IC Sync Log", {"source_name": si_return.name}))

	def test_pos_si_ignored(self):
		from intercompany_sync.jobs import on_sales_invoice_submit

		dn = make_dn()
		make_pr(dn.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si = make_si(dn.name)

		si.is_pos = 1
		# Note: Tested with submitted SI whose is_pos flag is set in memory as full POS setup requires POS Profile
		on_sales_invoice_submit(si)

		self.assertIsNone(get_pi(si.name))
		self.assertFalse(
			frappe.db.exists(
				"IC Sync Log",
				{"source_name": si.name, "action": "create_purchase_invoice"},
			)
		)

	def test_stale_source_returns_none_no_log(self):
		from intercompany_sync.jobs import create_purchase_invoice

		dn = make_dn()
		make_pr(dn.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si = make_si(dn.name)

		si.reload()
		si.cancel()
		self.assertEqual(si.docstatus, 2)

		log_count_before = frappe.db.count("IC Sync Log")
		result = create_purchase_invoice(si.name)
		self.assertIsNone(result)
		self.assertEqual(frappe.db.count("IC Sync Log"), log_count_before)
