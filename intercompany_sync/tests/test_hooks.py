"""Tests for intercompany_sync hooks configuration."""

import unittest

from intercompany_sync import hooks


class TestHooks(unittest.TestCase):
	def test_doctype_js_contains_all_four_intercompany_doctypes(self):
		"""Test doctype_js includes SO, PO, DN, PR pointing to shared intercompany_documents.js."""
		expected_path = "public/js/intercompany_documents.js"
		self.assertIn("Sales Order", hooks.doctype_js)
		self.assertIn(expected_path, hooks.doctype_js["Sales Order"])
		self.assertIn("Purchase Order", hooks.doctype_js)
		self.assertIn(expected_path, hooks.doctype_js["Purchase Order"])
		self.assertIn("Delivery Note", hooks.doctype_js)
		self.assertEqual(hooks.doctype_js["Delivery Note"], expected_path)
		self.assertIn("Purchase Receipt", hooks.doctype_js)
		self.assertEqual(hooks.doctype_js["Purchase Receipt"], expected_path)

	def test_doctype_js_contains_order_update_pricing_for_so_and_po(self):
		"""Test doctype_js includes order_update_pricing.js for Sales Order and Purchase Order."""
		expected_path = "public/js/order_update_pricing.js"
		self.assertIn(expected_path, hooks.doctype_js["Sales Order"])
		self.assertIn(expected_path, hooks.doctype_js["Purchase Order"])

	def test_doctype_js_preserves_existing_sales_invoice(self):
		"""Test existing Sales Invoice entry in doctype_js is preserved."""
		self.assertEqual(hooks.doctype_js.get("Sales Invoice"), "public/js/sales_invoice.js")

	def test_doc_events_preserves_purchase_receipt_and_automations(self):
		"""Test PR before_submit and existing doc_events remain intact."""
		pr_events = hooks.doc_events.get("Purchase Receipt", {})
		self.assertEqual(pr_events.get("validate"), "intercompany_sync.guards.validate_purchase_receipt")
		self.assertEqual(
			pr_events.get("before_submit"),
			"intercompany_sync.guards.before_submit_purchase_receipt",
		)
		self.assertIn("Sales Invoice", hooks.doc_events)
		self.assertIn("Delivery Note", hooks.doc_events)
		self.assertIn("Payment Entry", hooks.doc_events)

	def test_override_doctype_dashboards_registered_for_all_four_doctypes(self):
		"""Test override_doctype_dashboards registers pure enrichers for PO, SO, DN, and PR."""
		self.assertTrue(hasattr(hooks, "override_doctype_dashboards"))
		expected = {
			"Purchase Order": "intercompany_sync.connections.purchase_order",
			"Sales Order": "intercompany_sync.connections.sales_order",
			"Delivery Note": "intercompany_sync.connections.delivery_note",
			"Purchase Receipt": "intercompany_sync.connections.purchase_receipt",
		}
		for dt, method in expected.items():
			self.assertIn(dt, hooks.override_doctype_dashboards)
			self.assertEqual(hooks.override_doctype_dashboards[dt], method)



if __name__ == "__main__":
	unittest.main()
