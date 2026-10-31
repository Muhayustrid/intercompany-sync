"""Isolated contract tests for Task 2: Partial PR Safety.

Verifies:
- DN 100 -> PR60 -> mapper40 -> PR40
- Over-receipt rejected
- Cancel PR60 restores 60 availability
- Physical receipt accounting (Accepted + Rejected) in stock units
- Duplicate child row references aggregated
- Two drafts submitted with changed remaining balance (only within balance passes)
- Deterministic DN locking + current read after lock (for_update)
- Transaction UOM restriction matching DN UOM (preventing native status updater corruption)
- Linked PO UOM matching
- Malformed, negative, and nonfinite quantities / factors rejected
- Warehouse belonging to receiving company validated
- Return and non-internal receipts untouched
- Preserves existing SI/PI/DN cancel guards

Runnable without Frappe on the host using lightweight dependency boundary stubs.
Restores sys.modules on teardown to prevent side effects.
"""

import sys
import unittest
from types import ModuleType
from unittest.mock import MagicMock

_ORIGINAL_MODULES = dict(sys.modules)

frappe = None


class FakeFrappeDict(dict):
	def __getattribute__(self, key):
		if key != "__dict__" and key in self:
			return self[key]
		return super().__getattribute__(key)

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		self[key] = value

	def __hash__(self):
		return hash(self.get("name") or id(self))

	def __eq__(self, other):
		if isinstance(other, FakeFrappeDict):
			return self.get("name") == other.get("name") and dict(self) == dict(other)
		return False


class FakeDoc(FakeFrappeDict):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		if "items" not in self:
			self["items"] = []
		self.flags = FakeFrappeDict()

	def get(self, key, default=None):
		return super().get(key, default)

	def append(self, key, value):
		if key not in self:
			self[key] = []
		if isinstance(value, dict) and not isinstance(value, FakeDoc):
			value = FakeDoc(value)
		self[key].append(value)
		return value


def _setup_fake_frappe():
	fake_frappe = ModuleType("frappe")
	fake_frappe._is_fake = True
	fake_frappe._dict = FakeFrappeDict

	class PermissionError(Exception):
		pass

	class ValidationError(Exception):
		pass

	fake_frappe.PermissionError = PermissionError
	fake_frappe.ValidationError = ValidationError

	def _(text, *args):
		return text

	fake_frappe._ = _

	def throw(msg, exc=ValidationError, title=None):
		raise exc(msg)

	fake_frappe.throw = throw
	fake_frappe.msgprint = MagicMock()

	def whitelist(*args, **kwargs):
		def decorator(fn):
			return fn

		return decorator

	fake_frappe.whitelist = whitelist

	class FakeDB:
		def __init__(self):
			self._data = {}
			self._sql_calls = []

		def get_value(self, doctype, filters, fieldname=None, as_dict=False):
			return None

		def get_cached_value(self, doctype, name, fieldname):
			return self.get_value(doctype, name, fieldname)

		def get_all(self, doctype, **kwargs):
			return []

		def get_values(self, doctype, filters=None, fieldname=None, as_dict=False, for_update=False):
			return self.get_all(doctype, filters=filters, fields=fieldname)

		def exists(self, doctype, filters):
			return False

		def sql(self, query, values=None, as_dict=False):
			self._sql_calls.append((query, values))
			if "tabDelivery Note Item" in query and values:
				parent_name = values[0]
				dn = fake_frappe._docs_store.get(("Delivery Note", parent_name))
				if dn:
					return [dict(d) for d in dn.get("items", [])]
				return []
			elif "tabDelivery Note" in query and values:
				dn_name = values[0]
				dn = fake_frappe._docs_store.get(("Delivery Note", dn_name))
				if dn:
					return [dict(dn)]
				return []
			return []

	fake_frappe.FakeDB = FakeDB
	fake_frappe.db = FakeDB()
	fake_frappe.get_all = lambda doctype, **kwargs: fake_frappe.db.get_all(doctype, **kwargs)
	fake_frappe.get_cached_value = (
		lambda doctype, name, fieldname: fake_frappe.db.get_cached_value(doctype, name, fieldname)
	)

	def has_permission(doctype, ptype="read", doc=None, user=None, throw=False, print_logs=True):
		return True

	fake_frappe.has_permission = has_permission

	def get_doc(*args, **kwargs):
		if args and isinstance(args[0], dict):
			return FakeDoc(args[0])
		if args and isinstance(args[0], str) and len(args) > 1 and isinstance(args[1], str):
			return fake_frappe._docs_store.get((args[0], args[1]))
		return FakeDoc(kwargs)

	fake_frappe.get_doc = get_doc
	fake_frappe._docs_store = {}

	# Utils
	utils = ModuleType("frappe.utils")
	utils.flt = lambda v, precision=None: round(float(v or 0), precision) if precision is not None else float(v or 0)
	utils.cint = lambda v: int(v or 0)
	utils.cstr = lambda v: str(v or "")
	fake_frappe.utils = utils
	fake_frappe.flt = utils.flt
	fake_frappe.cint = utils.cint
	fake_frappe.cstr = utils.cstr

	sys.modules["frappe"] = fake_frappe
	sys.modules["frappe.utils"] = utils


def tearDownModule():
	for mod in list(sys.modules):
		if mod not in _ORIGINAL_MODULES:
			del sys.modules[mod]


class TestPartialReceiptGuards(unittest.TestCase):
	def tearDown(self):
		for mod in list(sys.modules):
			if mod not in _ORIGINAL_MODULES:
				del sys.modules[mod]

	def setUp(self):
		_setup_fake_frappe()
		global frappe
		import frappe as _frappe

		frappe = _frappe

		import importlib

		if "intercompany_sync.mapping" in sys.modules:
			self.mapping = importlib.reload(sys.modules["intercompany_sync.mapping"])
		else:
			import intercompany_sync.mapping

			self.mapping = intercompany_sync.mapping

		if "intercompany_sync.guards" in sys.modules:
			self.guards = importlib.reload(sys.modules["intercompany_sync.guards"])
		else:
			import intercompany_sync.guards

			self.guards = intercompany_sync.guards

		# Setup standard world entities
		self.outlet_company = "Outlet Co"
		self.supplier_company = "Supplier Co"
		self.internal_supplier = "Supplier Internal"
		self.internal_customer = "Customer Internal"
		self.item_code = "ITEM-001"
		self.warehouse = "Stores - OC"

		self.parties = {
			("Supplier", self.internal_supplier): {
				"is_internal_supplier": 1,
				"represents_company": self.supplier_company,
			},
			("Customer", self.internal_customer): {
				"is_internal_customer": 1,
				"represents_company": self.outlet_company,
			},
		}

		self.warehouses = {
			self.warehouse: {"company": self.outlet_company},
			"Stores - External": {"company": "External Co"},
		}

		def default_get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Supplier":
				key = filters if isinstance(filters, str) else filters.get("name")
				info = self.parties.get(("Supplier", key), {})
				if fieldname:
					return info.get(fieldname)
				return info
			elif doctype == "Customer":
				key = filters if isinstance(filters, str) else filters.get("name")
				info = self.parties.get(("Customer", key), {})
				if fieldname:
					return info.get(fieldname)
				return info
			elif doctype == "Warehouse":
				key = filters if isinstance(filters, str) else filters.get("name")
				info = self.warehouses.get(key, {})
				if fieldname:
					return info.get(fieldname)
				return info
			elif doctype == "Purchase Order Item":
				key = filters if isinstance(filters, str) else filters.get("name")
				po_items = getattr(self, "po_items_store", {})
				info = po_items.get(key, {})
				if fieldname:
					return info.get(fieldname)
				return info
			return None

		frappe.db.get_value = default_get_value
		frappe.db.get_cached_value = (
			lambda dt, name, fld: default_get_value(dt, name, fld)
		)

	def _create_dn(self, name="DN-001", qty=100, uom="Nos", stock_uom="Nos", cf=1.0, docstatus=1, is_return=0):
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": name,
				"company": self.supplier_company,
				"customer": self.internal_customer,
				"docstatus": docstatus,
				"is_return": is_return,
				"items": [
					FakeDoc(
						{
							"doctype": "Delivery Note Item",
							"name": f"{name}-ITEM-1",
							"item_code": self.item_code,
							"qty": qty,
							"stock_qty": qty * cf,
							"uom": uom,
							"stock_uom": stock_uom,
							"conversion_factor": cf,
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", name)] = dn
		return dn

	def _create_pr(
		self,
		dn_name="DN-001",
		name="PR-001",
		qty=60,
		rejected_qty=0,
		uom="Nos",
		stock_uom="Nos",
		cf=1.0,
		warehouse="Stores - OC",
		docstatus=0,
		is_return=0,
		is_internal=1,
		delivery_note_item=None,
		po_item=None,
	):
		if delivery_note_item is None:
			delivery_note_item = f"{dn_name}-ITEM-1"

		pr = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": name,
				"company": self.outlet_company,
				"supplier": self.internal_supplier,
				"is_internal_supplier": is_internal,
				"inter_company_reference": dn_name,
				"supplier_delivery_note": dn_name,
				"docstatus": docstatus,
				"is_return": is_return,
				"items": [
					FakeDoc(
						{
							"doctype": "Purchase Receipt Item",
							"name": f"{name}-ITEM-1",
							"parent": name,
							"item_code": self.item_code,
							"delivery_note_item": delivery_note_item,
							"qty": qty,
							"rejected_qty": rejected_qty,
							"uom": uom,
							"stock_uom": stock_uom,
							"conversion_factor": cf,
							"warehouse": warehouse,
							"purchase_order_item": po_item,
						}
					)
				],
			}
		)
		frappe._docs_store[("Purchase Receipt", name)] = pr
		return pr

	# 1. Non-internal supplier bypass
	def test_non_internal_supplier_bypasses_guard(self):
		pr = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": "PR-EXT",
				"company": self.outlet_company,
				"supplier": "Third Party Supplier",
				"is_internal_supplier": 0,
				"is_return": 0,
				"items": [FakeDoc({"item_code": "EXT-ITEM", "qty": 10})],
			}
		)
		# Should not throw even without inter_company_reference or DN
		self.guards.validate_purchase_receipt(pr)
		if hasattr(self.guards, "before_submit_purchase_receipt"):
			self.guards.before_submit_purchase_receipt(pr)

	# 2. Return PR bypass
	def test_return_pr_bypasses_guard(self):
		pr = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": "PR-RET",
				"company": self.outlet_company,
				"supplier": self.internal_supplier,
				"is_internal_supplier": 1,
				"is_return": 1,
				"items": [FakeDoc({"item_code": "RET-ITEM", "qty": 5})],
			}
		)
		self.guards.validate_purchase_receipt(pr)
		if hasattr(self.guards, "before_submit_purchase_receipt"):
			self.guards.before_submit_purchase_receipt(pr)

	# 3. Missing inter_company_reference
	def test_missing_inter_company_reference_throws(self):
		pr = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": "PR-NO-REF",
				"company": self.outlet_company,
				"supplier": self.internal_supplier,
				"is_internal_supplier": 1,
				"is_return": 0,
				"inter_company_reference": None,
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("Delivery Note", str(ctx.exception))

	# 4. DN missing or unsubmitted or return
	def test_unsubmitted_or_return_dn_throws(self):
		self._create_dn("DN-DRAFT", docstatus=0)
		pr1 = self._create_pr("DN-DRAFT", "PR-DRAFT")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr1)
		self.assertIn("tersubmit", str(ctx.exception).lower())

		self._create_dn("DN-RET", docstatus=1, is_return=1)
		pr2 = self._create_pr("DN-RET", "PR-RET-DN")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr2)
		self.assertIn("retur", str(ctx.exception).lower())

	# 5. Party / company mismatch
	def test_party_company_mismatch_throws(self):
		self._create_dn("DN-PARTY-MISMATCH")
		# Mismatch customer on DN
		self.parties[("Customer", self.internal_customer)]["represents_company"] = "Other Company"
		pr = self._create_pr("DN-PARTY-MISMATCH")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("mewakili", str(ctx.exception).lower())

	# 6. Warehouse company validation
	def test_warehouse_company_mismatch_throws(self):
		self._create_dn("DN-WH")
		pr = self._create_pr("DN-WH", warehouse="Stores - External")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("Warehouse", str(ctx.exception))

	# 7. Exact row ref validation
	def test_exact_row_ref_missing_or_invalid_throws(self):
		self._create_dn("DN-ROW-REF")
		# Empty delivery_note_item
		pr1 = self._create_pr("DN-ROW-REF", delivery_note_item="")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr1)
		self.assertIn("delivery_note_item", str(ctx.exception))

		# Non-existent delivery_note_item
		pr2 = self._create_pr("DN-ROW-REF", delivery_note_item="NON-EXISTENT-DNI")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr2)
		self.assertIn("ditemukan", str(ctx.exception).lower())

	# 8. Item code & Stock UOM mismatch
	def test_item_code_and_stock_uom_mismatch_throws(self):
		self._create_dn("DN-MISMATCH")
		pr1 = self._create_pr("DN-MISMATCH")
		pr1.items[0].item_code = "ITEM-DIFFERENT"
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr1)
		self.assertIn("Item", str(ctx.exception))

		pr2 = self._create_pr("DN-MISMATCH")
		pr2.items[0].stock_uom = "Kg"
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr2)
		self.assertIn("Stock UOM", str(ctx.exception))

	# 9. Transaction UOM mismatch with DN UOM (prevent status updater corruption)
	def test_transaction_uom_mismatch_with_dn_throws(self):
		self._create_dn("DN-UOM", uom="Nos", stock_uom="Nos")
		pr = self._create_pr("DN-UOM", uom="Box", stock_uom="Nos", cf=10.0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("UOM", str(ctx.exception))

	# 10. Linked PO UOM mismatch
	def test_linked_po_uom_mismatch_throws(self):
		self._create_dn("DN-PO-UOM")
		self.po_items_store = {
			"POI-001": {"name": "POI-001", "uom": "Box"}
		}
		pr = self._create_pr("DN-PO-UOM", uom="Nos", po_item="POI-001")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("Purchase Order", str(ctx.exception))

	# 11. Malformed, negative, nonfinite qty and factor
	def test_malformed_negative_nonfinite_qty_and_factor_throws(self):
		self._create_dn("DN-MALFORMED")

		# Negative qty
		pr1 = self._create_pr("DN-MALFORMED", qty=-10)
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr1)

		# Negative rejected_qty
		pr2 = self._create_pr("DN-MALFORMED", qty=10, rejected_qty=-1)
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr2)

		# Zero or negative factor
		pr3 = self._create_pr("DN-MALFORMED", qty=10, cf=0)
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr3)

		# Non-finite qty
		pr4 = self._create_pr("DN-MALFORMED", qty=float("nan"))
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr4)

		# Non-finite conversion_factor
		pr5 = self._create_pr("DN-MALFORMED", qty=10, cf=float("inf"))
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr5)

		# Zero total receipt
		pr6 = self._create_pr("DN-MALFORMED", qty=0, rejected_qty=0)
		with self.assertRaises(frappe.ValidationError):
			self.guards.validate_purchase_receipt(pr6)

	# 12. Regresi partial receipt: DN 100 -> PR60 -> PR40; overreceipt blocked; cancel restores 60
	def test_partial_receipt_cycle_and_cancellation(self):
		dn = self._create_dn("DN-CYCLE", qty=100)
		submitted_pr_items = []

		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				dni = dn.items[0].name
				return [p for p in submitted_pr_items if p.get("delivery_note_item") == dni and p.get("docstatus") == 1]
			return []

		frappe.db.get_all = fake_get_all

		# Step 1: PR1 for 60 submitted
		pr1 = self._create_pr("DN-CYCLE", "PR-60", qty=60)
		self.guards.before_submit_purchase_receipt(pr1)
		# Simulate submit
		pr1.docstatus = 1
		submitted_pr_items.append(
			FakeDoc(
				{
					"parent": pr1.name,
					"delivery_note_item": dn.items[0].name,
					"qty": 60,
					"rejected_qty": 0,
					"conversion_factor": 1.0,
					"received_stock_qty": 60,
					"docstatus": 1,
				}
			)
		)

		# Step 2: PR2 for 50 attempts submit -> over-receipt rejected (only 40 left)
		pr_over = self._create_pr("DN-CYCLE", "PR-OVER", qty=50)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.before_submit_purchase_receipt(pr_over)
		self.assertIn("melebihi", str(ctx.exception).lower())

		# Step 3: PR2 for 40 attempts submit -> succeeds!
		pr2 = self._create_pr("DN-CYCLE", "PR-40", qty=40)
		self.guards.before_submit_purchase_receipt(pr2)
		pr2.docstatus = 1
		submitted_pr_items.append(
			FakeDoc(
				{
					"parent": pr2.name,
					"delivery_note_item": dn.items[0].name,
					"qty": 40,
					"rejected_qty": 0,
					"conversion_factor": 1.0,
					"received_stock_qty": 40,
					"docstatus": 1,
				}
			)
		)

		# Step 4: Now DN is fully received (60 + 40 = 100). Any further PR rejected
		pr_more = self._create_pr("DN-CYCLE", "PR-MORE", qty=1)
		with self.assertRaises(frappe.ValidationError):
			self.guards.before_submit_purchase_receipt(pr_more)

		# Step 5: Cancel PR1 (60). Availability is restored to 60!
		pr1.docstatus = 2
		# Update submitted_pr_items to reflect cancellation (docstatus=2)
		for item in submitted_pr_items:
			if item.parent == pr1.name:
				item.docstatus = 2

		# Now a new PR for 60 can submit!
		pr_new60 = self._create_pr("DN-CYCLE", "PR-NEW60", qty=60)
		self.guards.before_submit_purchase_receipt(pr_new60)  # Must pass without error!

	# 13. Physical receipt: Accepted + Rejected in stock units
	def test_physical_receipt_accepted_plus_rejected_stock_units(self):
		dn = self._create_dn("DN-ACC-REJ", qty=100)
		submitted_pr_items = []

		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				return [p for p in submitted_pr_items if p.get("docstatus") == 1]
			return []

		frappe.db.get_all = fake_get_all

		# PR1 has accepted 40, rejected 20 -> physical received = 60 stock units
		pr1 = self._create_pr("DN-ACC-REJ", "PR-PHY", qty=40, rejected_qty=20)
		self.guards.before_submit_purchase_receipt(pr1)
		pr1.docstatus = 1
		submitted_pr_items.append(
			FakeDoc(
				{
					"parent": pr1.name,
					"delivery_note_item": dn.items[0].name,
					"qty": 40,
					"rejected_qty": 20,
					"conversion_factor": 1.0,
					"received_stock_qty": 60,
					"docstatus": 1,
				}
			)
		)

		# Remaining is 40. PR asking for 50 must be rejected
		pr_fail = self._create_pr("DN-ACC-REJ", "PR-FAIL", qty=50)
		with self.assertRaises(frappe.ValidationError):
			self.guards.before_submit_purchase_receipt(pr_fail)

		# PR asking for 40 must succeed
		pr_ok = self._create_pr("DN-ACC-REJ", "PR-OK", qty=40)
		self.guards.before_submit_purchase_receipt(pr_ok)

	# 14. Duplicate child references aggregated in current PR
	def test_duplicate_child_references_aggregated(self):
		dn = self._create_dn("DN-DUP", qty=100)
		dni = dn.items[0].name

		# PR has two rows pointing to the same DN item: 30 + 40 = 70 (<= 100)
		pr_ok = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": "PR-DUP-OK",
				"company": self.outlet_company,
				"supplier": self.internal_supplier,
				"is_internal_supplier": 1,
				"inter_company_reference": dn.name,
				"docstatus": 0,
				"is_return": 0,
				"items": [
					FakeDoc(
						{
							"delivery_note_item": dni,
							"item_code": self.item_code,
							"qty": 30,
							"rejected_qty": 0,
							"uom": "Nos",
							"stock_uom": "Nos",
							"conversion_factor": 1.0,
							"warehouse": self.warehouse,
						}
					),
					FakeDoc(
						{
							"delivery_note_item": dni,
							"item_code": self.item_code,
							"qty": 40,
							"rejected_qty": 0,
							"uom": "Nos",
							"stock_uom": "Nos",
							"conversion_factor": 1.0,
							"warehouse": self.warehouse,
						}
					),
				],
			}
		)
		self.guards.before_submit_purchase_receipt(pr_ok)

		# PR has two rows: 30 + 80 = 110 (> 100) -> must be rejected!
		pr_fail = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"name": "PR-DUP-FAIL",
				"company": self.outlet_company,
				"supplier": self.internal_supplier,
				"is_internal_supplier": 1,
				"inter_company_reference": dn.name,
				"docstatus": 0,
				"is_return": 0,
				"items": [
					FakeDoc(
						{
							"delivery_note_item": dni,
							"item_code": self.item_code,
							"qty": 30,
							"rejected_qty": 0,
							"uom": "Nos",
							"stock_uom": "Nos",
							"conversion_factor": 1.0,
							"warehouse": self.warehouse,
						}
					),
					FakeDoc(
						{
							"delivery_note_item": dni,
							"item_code": self.item_code,
							"qty": 80,
							"rejected_qty": 0,
							"uom": "Nos",
							"stock_uom": "Nos",
							"conversion_factor": 1.0,
							"warehouse": self.warehouse,
						}
					),
				],
			}
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.before_submit_purchase_receipt(pr_fail)
		self.assertIn("melebihi", str(ctx.exception).lower())

	# 15. Two drafts race: only submission within remaining balance passes
	def test_two_drafts_race_only_within_remaining_passes(self):
		dn = self._create_dn("DN-RACE", qty=100)
		submitted_pr_items = []

		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				return [p for p in submitted_pr_items if p.get("docstatus") == 1]
			return []

		frappe.db.get_all = fake_get_all

		# Draft A for 60
		draft_a = self._create_pr("DN-RACE", "PR-DRAFT-A", qty=60)
		# Draft B for 60
		draft_b = self._create_pr("DN-RACE", "PR-DRAFT-B", qty=60)

		# Both validate cleanly as drafts
		self.guards.validate_purchase_receipt(draft_a)
		self.guards.validate_purchase_receipt(draft_b)

		# Submit Draft A first -> succeeds
		self.guards.before_submit_purchase_receipt(draft_a)
		draft_a.docstatus = 1
		submitted_pr_items.append(
			FakeDoc(
				{
					"parent": draft_a.name,
					"delivery_note_item": dn.items[0].name,
					"qty": 60,
					"rejected_qty": 0,
					"conversion_factor": 1.0,
					"received_stock_qty": 60,
					"docstatus": 1,
				}
			)
		)

		# Submit Draft B now -> remaining balance changed to 40, Draft B asks for 60 -> REJECTED!
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.before_submit_purchase_receipt(draft_b)
		self.assertIn("melebihi", str(ctx.exception).lower())

	# 16. Deterministic DN row locking and current read after lock
	def test_deterministic_row_locking_and_current_read(self):
		self._create_dn("DN-LOCK", qty=100)
		pr = self._create_pr("DN-LOCK", "PR-LOCK", qty=50)

		self.guards.before_submit_purchase_receipt(pr)

		# Verify SQL lock statements were executed
		sql_queries = [call[0] for call in frappe.db._sql_calls]
		dn_parent_locked = any(
			"tabDelivery Note" in q and "FOR UPDATE" in q for q in sql_queries
		)
		dn_items_locked = any(
			"tabDelivery Note Item" in q and "FOR UPDATE" in q and "ORDER BY" in q
			for q in sql_queries
		)
		self.assertTrue(dn_parent_locked, "Delivery Note parent row must be locked with FOR UPDATE")
		self.assertTrue(dn_items_locked, "Delivery Note Item rows must be locked deterministically")

	# 17. get_outstanding_dn_rows for_update keyword parameter
	def test_get_outstanding_dn_rows_for_update_param(self):
		dn = self._create_dn("DN-FOR-UPDATE", qty=100)
		dni = dn.items[0].name

		captured_args = {}

		def fake_get_values(doctype, filters=None, fieldname=None, as_dict=False, for_update=False):
			captured_args["doctype"] = doctype
			captured_args["for_update"] = for_update
			return []

		frappe.db.get_values = fake_get_values

		# Default for_update=False uses normal read
		out1 = self.mapping.get_outstanding_dn_rows(dn)
		self.assertEqual(out1[dni]["stock_qty"], 100.0)

		# for_update=True uses locking read
		out2 = self.mapping.get_outstanding_dn_rows(dn, for_update=True)
		self.assertEqual(out2[dni]["stock_qty"], 100.0)
		self.assertTrue(captured_args.get("for_update"), "get_outstanding_dn_rows(..., for_update=True) must request locking read")

	# 18. Existing SI/PI/DN cancel guards preserved
	def test_existing_cancel_and_si_guards_preserved(self):
		# validate_sales_invoice: pos/return skipped
		si_ret = FakeDoc({"is_return": 1, "is_pos": 0, "is_internal_customer": 1})
		self.guards.validate_sales_invoice(si_ret)

		# validate_delivery_note_cancel: return skipped
		dn_ret = FakeDoc({"is_return": 1, "is_internal_customer": 1})
		self.guards.validate_delivery_note_cancel(dn_ret)

		# validate_purchase_invoice_cancel: ic_cascade skipped
		pi_casc = FakeDoc({"flags": FakeFrappeDict({"ic_cascade": True}), "inter_company_invoice_reference": "SI-001"})
		self.guards.validate_purchase_invoice_cancel(pi_casc)

	# 19. Finding 1: Current locked read of DN header/items after lock, and DN cancel protocol lock
	def test_finding_1_dn_header_and_items_current_locked_read_and_cancel_lock(self):
		self._create_dn("DN-CURRENT-LOCK", qty=100)
		pr = self._create_pr("DN-CURRENT-LOCK", "PR-CURRENT-LOCK", qty=50)

		# Execute before_submit
		self.guards.before_submit_purchase_receipt(pr)

		# Verify SQL lock statements locked both header and child rows with columns (current read)
		sql_queries = [call[0] for call in frappe.db._sql_calls]
		dn_header_locked = any(
			"tabDelivery Note" in q and "FOR UPDATE" in q for q in sql_queries
		)
		dn_items_locked = any(
			"tabDelivery Note Item" in q and "FOR UPDATE" in q and "ORDER BY" in q for q in sql_queries
		)
		self.assertTrue(dn_header_locked, "DN header must be locked with FOR UPDATE")
		self.assertTrue(dn_items_locked, "DN items must be locked with FOR UPDATE in deterministic order")

		# Test DN cancel protocol: validate_delivery_note_cancel locks DN parent and does current-read for submitted PR
		frappe.db._sql_calls.clear()

		# Define fake get_values / sql for submitted PR search under lock
		dn_to_cancel = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-TO-CANCEL",
				"company": self.supplier_company,
				"customer": self.internal_customer,
				"is_return": 0,
				"is_internal_customer": 1,
			}
		)
		# Case A: When counterpart PR is submitted, cancel must be blocked under lock
		def fake_sql_cancel(query, values=None, as_dict=False):
			frappe.db._sql_calls.append((query, values))
			if "tabPurchase Receipt" in query:
				return [{"name": "PR-SUBMITTED-001"}]
			return []

		frappe.db.sql = fake_sql_cancel
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_delivery_note_cancel(dn_to_cancel)
		self.assertIn("sudah diterima", str(ctx.exception).lower())

		# Verify DN parent was locked before/during cancel check
		cancel_queries = [call[0] for call in frappe.db._sql_calls]
		self.assertTrue(
			any("tabDelivery Note" in q and "FOR UPDATE" in q for q in cancel_queries),
			"validate_delivery_note_cancel must lock DN parent with FOR UPDATE"
		)
		pr_queries = [q for q in cancel_queries if "tabPurchase Receipt" in q]
		self.assertTrue(pr_queries, "Counterpart PR check query must be executed")
		pr_query = pr_queries[0]
		self.assertNotIn("FOR UPDATE LIMIT", pr_query, "FOR UPDATE LIMIT is invalid MariaDB syntax")
		if "LIMIT 1" in pr_query and "FOR UPDATE" in pr_query:
			self.assertLess(
				pr_query.index("LIMIT 1"),
				pr_query.index("FOR UPDATE"),
				"LIMIT 1 must precede FOR UPDATE in MariaDB SQL syntax"
			)

	# 20. Finding 2: Same UOM but different conversion factor rejected at native precision 9
	def test_finding_2_conversion_factor_must_equal_dn_factor(self):
		self._create_dn("DN-CF", qty=10, uom="Box", stock_uom="Nos", cf=10.0)
		# PR has same UOM ("Box"), but conversion factor is 5.0 instead of 10.0
		pr_diff_cf = self._create_pr("DN-CF", "PR-DIFF-CF", qty=10, uom="Box", stock_uom="Nos", cf=5.0)

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr_diff_cf)
		self.assertIn("conversion factor", str(ctx.exception).lower())

		# Native precision 9: 1.000000001 vs 1.000000499 differ at 9 decimal places
		self._create_dn("DN-CF-9", qty=10, uom="Box", stock_uom="Nos", cf=1.000000001)
		pr_sub_micro = self._create_pr("DN-CF-9", "PR-SUB-MICRO", qty=5, uom="Box", stock_uom="Nos", cf=1.000000499)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr_sub_micro)
		self.assertIn("conversion factor", str(ctx.exception).lower())

	# 21. Finding 3: If item.delivery_note provided and differs from header, reject
	def test_finding_3_optional_item_delivery_note_parent_mismatch_rejected(self):
		self._create_dn("DN-HEADER-A", qty=100)
		pr = self._create_pr("DN-HEADER-A", "PR-MISMATCH-PARENT", qty=50)

		# Case A: item.delivery_note is empty/None -> allowed
		pr.items[0].delivery_note = None
		self.guards.validate_purchase_receipt(pr)

		# Case B: item.delivery_note matches header -> allowed
		pr.items[0].delivery_note = "DN-HEADER-A"
		self.guards.validate_purchase_receipt(pr)

		# Case C: item.delivery_note differs from header -> rejected!
		pr.items[0].delivery_note = "DN-HEADER-DIFF"
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(pr)
		self.assertIn("delivery note", str(ctx.exception).lower())

	# 22. Finding 4: Draft validate checks aggregate <= original dispatched stock
	def test_finding_4_draft_validate_checks_original_dispatched_capacity(self):
		self._create_dn("DN-ORIG-CAP", qty=100)

		# Two drafts with qty 60 each (within original 100) both pass validate_purchase_receipt
		draft1 = self._create_pr("DN-ORIG-CAP", "PR-DRAFT-1", qty=60)
		draft2 = self._create_pr("DN-ORIG-CAP", "PR-DRAFT-2", qty=60)
		self.guards.validate_purchase_receipt(draft1)
		self.guards.validate_purchase_receipt(draft2)

		# Draft with qty 120 (> original 100) fails validate_purchase_receipt
		draft_over = self._create_pr("DN-ORIG-CAP", "PR-DRAFT-OVER", qty=120)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.guards.validate_purchase_receipt(draft_over)
		self.assertIn("melebihi", str(ctx.exception).lower())

	def test_finding_2_unauthorized_dn_raises_generic_permission_error_without_leaks(self):
		"""Finding 2: PR referencing unauthorized DN raises generic PermissionError without revealing DN details."""
		self._create_dn("DN-CONFIDENTIAL", qty=10)
		pr = self._create_pr("DN-CONFIDENTIAL", "PR-UNAUTH", qty=5)

		original_has_perm = frappe.has_permission
		captured_calls = []

		def restricted_has_perm(doctype, ptype="read", doc=None, user=None, throw=False, print_logs=True):
			captured_calls.append({
				"doctype": doctype,
				"ptype": ptype,
				"doc": doc,
				"print_logs": print_logs,
			})
			if doctype == "Delivery Note" and ptype == "read" and doc == "DN-CONFIDENTIAL":
				if print_logs:
					frappe.msgprint(f"No read permission for {doctype} {doc}")
				return False
			return original_has_perm(doctype, ptype, doc=doc, user=user, throw=throw, print_logs=print_logs)

		frappe.has_permission = restricted_has_perm
		try:
			frappe.msgprint.reset_mock()
			# 1. validate_purchase_receipt must raise PermissionError
			with self.assertRaises(frappe.PermissionError) as ctx:
				self.guards.validate_purchase_receipt(pr)
			err_msg = str(ctx.exception)
			self.assertNotIn("DN-CONFIDENTIAL", err_msg)
			self.assertNotIn(self.internal_customer, err_msg)
			self.assertNotIn(self.supplier_company, err_msg)
			self.assertNotIn(self.item_code, err_msg)
			self.assertEqual(len(captured_calls), 1)
			self.assertIs(captured_calls[0]["print_logs"], False, "validate_purchase_receipt must pass print_logs=False")
			frappe.msgprint.assert_not_called()

			# 2. before_submit_purchase_receipt must raise PermissionError without reading/locking
			captured_calls.clear()
			frappe.msgprint.reset_mock()
			with self.assertRaises(frappe.PermissionError) as ctx_submit:
				self.guards.before_submit_purchase_receipt(pr)
			err_msg_submit = str(ctx_submit.exception)
			self.assertNotIn("DN-CONFIDENTIAL", err_msg_submit)
			self.assertNotIn(self.internal_customer, err_msg_submit)
			self.assertNotIn(self.supplier_company, err_msg_submit)
			self.assertNotIn(self.item_code, err_msg_submit)
			self.assertEqual(len(captured_calls), 1)
			self.assertIs(captured_calls[0]["print_logs"], False, "before_submit_purchase_receipt must pass print_logs=False")
			frappe.msgprint.assert_not_called()
		finally:
			frappe.has_permission = original_has_perm
