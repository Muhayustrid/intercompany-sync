from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from intercompany_sync.jobs import create_juri_payment, create_purchase_invoice
from intercompany_sync.tests.factories import (
	get_pi,
	make_dn,
	make_pr,
	make_si,
	pay_pis,
	setup_world,
)


class TestSettings(IntegrationTestCase):
	def setUp(self):
		setup_world()
		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

	def tearDown(self):
		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

	def test_toggle_disable_pi_sync_blocks_enqueue_and_creation(self):
		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 1)
		dn = make_dn()
		make_pr(dn.name)

		with patch("frappe.enqueue") as mock_enqueue:
			si = make_si(dn.name)
			mock_enqueue.assert_not_called()

		self.assertIsNone(get_pi(si.name))
		logs = frappe.get_all(
			"IC Sync Log",
			filters={"source_name": si.name},
		)
		self.assertEqual(len(logs), 0)

	def test_toggle_disable_juri_pe_sync_blocks_enqueue_and_creation(self):
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 1)
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)

		with patch("frappe.enqueue") as mock_enqueue:
			pe = pay_pis({pi.name: 2000})
			mock_enqueue.assert_not_called()

		pe.reload()
		self.assertFalse(bool(pe.ic_payment_reference))
		logs = frappe.get_all(
			"IC Sync Log",
			filters={"source_name": pe.name},
		)
		self.assertEqual(len(logs), 0)

	def test_toggle_unset_defaults_to_active(self):
		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)
		self.assertIsNotNone(pi)
		self.assertEqual(pi.docstatus, 1)

		pe = pay_pis({pi.name: 2000})
		pe.reload()
		self.assertTrue(bool(pe.ic_payment_reference))

	def test_pi_job_noop_when_toggle_enabled_after_enqueue(self):
		dn = make_dn()
		make_pr(dn.name)
		with patch("frappe.enqueue"):
			si = make_si(dn.name)

		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 1)

		result = create_purchase_invoice(si.name)
		self.assertIsNone(result)
		self.assertIsNone(get_pi(si.name))
		logs = frappe.get_all(
			"IC Sync Log",
			filters={"source_name": si.name},
		)
		self.assertEqual(len(logs), 0)

	def test_juri_pe_job_noop_when_toggle_enabled_after_enqueue(self):
		dn = make_dn()
		make_pr(dn.name)
		si = make_si(dn.name)
		pi = get_pi(si.name)

		with patch("frappe.enqueue"):
			pe = pay_pis({pi.name: 2000})

		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 1)

		result = create_juri_payment(pe.name)
		self.assertIsNone(result)
		pe.reload()
		self.assertFalse(bool(pe.ic_payment_reference))
		logs = frappe.get_all(
			"IC Sync Log",
			filters={"source_name": pe.name},
		)
		self.assertEqual(len(logs), 0)
