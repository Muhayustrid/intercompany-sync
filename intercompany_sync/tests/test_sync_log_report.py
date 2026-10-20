from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from intercompany_sync.tests.factories import (
	OUTLET_CO,
	SUPPLIER_CO,
	get_pi,
	make_dn,
	make_pr,
	make_si,
	pay_pis,
	setup_world,
)


class TestSetup(IntegrationTestCase):
	def test_world_and_field(self):
		setup_world()
		dn = make_dn()
		pr = make_pr(dn.name)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(pr.company, OUTLET_CO)
		self.assertTrue(frappe.get_meta("Payment Entry").has_field("ic_payment_reference"))
		log = frappe.get_doc(
			{
				"doctype": "IC Sync Log",
				"action": "create_purchase_invoice",
				"source_doctype": "Sales Invoice",
				"source_name": "X",
				"status": "Failed",
			}
		).insert()
		self.assertEqual(log.status, "Failed")

	def test_retry_after_failure(self):
		from intercompany_sync.intercompany_sync.doctype.ic_sync_log.ic_sync_log import retry

		setup_world()
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
			fields=["name", "status", "error"],
		)
		self.assertEqual(len(logs), 1)
		log_name = logs[0].name
		self.assertEqual(logs[0].status, "Failed")
		self.assertIn("boom", logs[0].error or "")

		pi_name = retry(log_name)
		self.assertIsNotNone(pi_name)

		pi = get_pi(si.name)
		self.assertIsNotNone(pi)
		self.assertEqual(pi.docstatus, 1)
		self.assertEqual(pi.name, pi_name)

		updated_log = frappe.get_doc("IC Sync Log", log_name)
		self.assertEqual(updated_log.status, "Success")

	def test_retry_stale_source_raises(self):
		from intercompany_sync.intercompany_sync.doctype.ic_sync_log.ic_sync_log import retry

		setup_world()
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
			fields=["name", "status"],
		)
		self.assertEqual(len(logs), 1)
		log_name = logs[0].name
		self.assertEqual(logs[0].status, "Failed")

		si.reload()
		si.cancel()
		self.assertEqual(si.docstatus, 2)

		with self.assertRaises(frappe.ValidationError):
			retry(log_name)

		self.assertIsNone(get_pi(si.name))

	def test_report_lists_gaps(self):
		from intercompany_sync.intercompany_sync.report.ic_reconciliation.ic_reconciliation import execute

		setup_world()

		# Normal SI creates PI automatically
		dn_normal = make_dn()
		make_pr(dn_normal.name)
		si_normal = make_si(dn_normal.name)

		# Gap SI: patch create_purchase_invoice so no PI is created
		dn_gap = make_dn()
		make_pr(dn_gap.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si_gap = make_si(dn_gap.name)

		columns, data = execute()
		self.assertEqual(len(columns), 5)
		issues_by_source = {d["source_name"]: d for d in data}

		# Normal SI should not appear
		self.assertNotIn(si_normal.name, issues_by_source)

		# Gap SI should appear as "SI tanpa PI"
		self.assertIn(si_gap.name, issues_by_source)
		gap_row = issues_by_source[si_gap.name]
		self.assertEqual(gap_row["issue"], "SI tanpa PI")
		self.assertEqual(gap_row["source_doctype"], "Sales Invoice")
		self.assertEqual(gap_row["source_name"], si_gap.name)

		# Also verify desk query_report.run
		run_res = frappe.desk.query_report.run("IC Reconciliation")
		run_issues = {d["source_name"]: d for d in run_res["result"]}
		self.assertNotIn(si_normal.name, run_issues)
		self.assertIn(si_gap.name, run_issues)

	def test_report_lists_pe_gap(self):
		from intercompany_sync.intercompany_sync.report.ic_reconciliation.ic_reconciliation import execute

		setup_world()
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		with patch("intercompany_sync.jobs.create_juri_payment"):
			pe = pay_pis({pi.name: 2000})

		_columns, data = execute()
		pe_issues = {d["source_name"]: d for d in data if d.get("source_doctype") == "Payment Entry"}
		self.assertIn(pe.name, pe_issues)
		self.assertEqual(pe_issues[pe.name]["issue"], "PE outlet tanpa PE JURI")

	def test_report_lists_selisih_nominal(self):
		from intercompany_sync.intercompany_sync.report.ic_reconciliation.ic_reconciliation import execute

		setup_world()
		dn = make_dn(qty=2, rate=1000)
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		frappe.db.set_value("Purchase Invoice", pi.name, "grand_total", 1500.0)

		_columns, data = execute()
		mismatches = [d for d in data if d["source_name"] == si.name and d["issue"] == "Selisih nominal"]
		self.assertEqual(len(mismatches), 1)
		self.assertEqual(mismatches[0]["counterpart"], pi.name)
		self.assertEqual(mismatches[0]["difference"], 500.0)

	def test_report_company_filter(self):
		from intercompany_sync.intercompany_sync.report.ic_reconciliation.ic_reconciliation import execute

		setup_world()
		dn = make_dn()
		make_pr(dn.name)
		with patch("intercompany_sync.jobs.create_purchase_invoice"):
			si_gap = make_si(dn.name)

		_, data_supplier = execute(filters={"company": SUPPLIER_CO})
		names_supplier = [d["source_name"] for d in data_supplier]
		self.assertIn(si_gap.name, names_supplier)

		_, data_outlet = execute(filters={"company": OUTLET_CO})
		names_outlet = [d["source_name"] for d in data_outlet]
		self.assertIn(si_gap.name, names_outlet)

		_, data_other = execute(filters={"company": "Nonexistent Co"})
		names_other = [d["source_name"] for d in data_other]
		self.assertNotIn(si_gap.name, names_other)

	def test_retry_pe_after_failure(self):
		from intercompany_sync.intercompany_sync.doctype.ic_sync_log.ic_sync_log import retry

		setup_world()
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		with patch(
			"intercompany_sync.jobs.get_payment_entry",
			side_effect=frappe.ValidationError("pe boom"),
		):
			pe = pay_pis({pi.name: 2000})

		logs = frappe.get_all(
			"IC Sync Log",
			filters={"action": "create_juri_payment", "source_name": pe.name},
			fields=["name", "status", "error"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Failed")

		juri_pe_name = retry(logs[0].name)
		self.assertIsNotNone(juri_pe_name)

		updated_log = frappe.get_doc("IC Sync Log", logs[0].name)
		self.assertEqual(updated_log.status, "Success")

