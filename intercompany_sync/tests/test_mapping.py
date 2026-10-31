"""Isolated contract tests for intercompany_sync.mapping.

Runnable without Frappe on the host using lightweight dependency boundary stubs.
Restores sys.modules on teardown to prevent side effects.
"""

import sys
import unittest
from types import ModuleType
from unittest.mock import MagicMock

# Track newly installed modules so we can clean up cleanly
_ORIGINAL_MODULES = dict(sys.modules)


def _find_erpnext_schema(subpath: str):
	"""Resolve an ERPNext schema file portably via package spec, environment vars, or bench paths."""
	import importlib.util
	import os
	from pathlib import Path

	# 1. Environment variable overrides
	for env_var in ("ERPNEXT_APPS_PATH", "ERPNEXT_PATH", "FRAPPE_BENCH_ROOT"):
		val = os.environ.get(env_var)
		if val:
			p = Path(val)
			for candidate in (
				p / subpath,
				p / "erpnext" / subpath,
				p / "apps" / "erpnext" / "erpnext" / subpath,
			):
				if candidate.is_file():
					return candidate

	# 2. Package spec if installed
	try:
		spec = importlib.util.find_spec("erpnext")
		if spec and spec.submodule_search_locations:
			for loc in spec.submodule_search_locations:
				candidate = Path(loc) / subpath
				if candidate.is_file():
					return candidate
	except (ValueError, AttributeError):
		pass

	# 3. Known bench and sibling locations
	cwd = Path.cwd().resolve()
	candidates = [
		cwd.parent / "erpnext" / "erpnext" / subpath,
		cwd.parents[1] / "erpnext" / "erpnext" / subpath,
		cwd.parents[2] / "erpnext" / "erpnext" / subpath,
	]
	for candidate in candidates:
		if candidate.is_file():
			return candidate

	return None


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

	def check_permission(self, permtype="read"):
		import frappe

		if not frappe.has_permission(self.doctype, permtype, self.name):
			raise frappe.PermissionError(f"No {permtype} permission for {self.doctype} {self.name}")


def _setup_fake_frappe():
	if "frappe" in sys.modules and not getattr(sys.modules["frappe"], "_is_fake", False):
		return

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
			self._exists_data = {}

		def get_value(self, doctype, filters, fieldname=None, as_dict=False):
			return None

		def get_all(self, doctype, **kwargs):
			return []

		def get_list(self, doctype, **kwargs):
			records = self.get_all(doctype, **kwargs)
			filters = kwargs.get("filters")
			if filters:
				filtered = []
				for r in records:
					if not isinstance(r, dict):
						filtered.append(r)
						continue
					match = True
					if isinstance(filters, dict):
						for k, v in filters.items():
							if isinstance(v, tuple) and v[0] == "in":
								if r.get(k) not in v[1]:
									match = False
									break
							elif r.get(k) != v:
								match = False
								break
					elif isinstance(filters, list):
						for cond in filters:
							field, op, val = cond[0], cond[1], cond[2]
							r_val = r.get(field)
							if (op == "=" and r_val != val) or (op == "in" and r_val not in val):
								match = False
								break
					if match:
						filtered.append(r)
				records = filtered

			or_filters = kwargs.get("or_filters")
			if or_filters:
				filtered = []
				for r in records:
					matched = False
					for cond in or_filters:
						field, op, val = cond[0], cond[1], cond[2]
						r_val = str(r.get(field) or "").lower()
						clean_val = str(val).replace("%", "").lower()
						if op == "like" and clean_val in r_val:
							matched = True
							break
					if matched:
						filtered.append(r)
				records = filtered

			start = kwargs.get("start", 0)
			page_len = kwargs.get("page_length")
			if page_len is not None:
				return records[start : start + page_len]
			return records[start:]

		def exists(self, doctype, filters):
			return False

		def set_value(self, doctype, name, fieldname, value):
			pass

	fake_frappe.FakeDB = FakeDB
	fake_frappe.db = FakeDB()
	fake_frappe.get_all = lambda doctype, **kwargs: fake_frappe.db.get_all(doctype, **kwargs)
	fake_frappe.get_list = lambda doctype, **kwargs: fake_frappe.db.get_list(doctype, **kwargs)
	fake_frappe.get_cached_value = (
		lambda doctype, name, fieldname: fake_frappe.db.get_value(doctype, name, fieldname)
	)

	def has_permission(doctype, ptype="read", doc=None, user=None, throw=False):
		return True

	fake_frappe.has_permission = has_permission

	def get_doc(*args, **kwargs):
		if args and isinstance(args[0], dict):
			return FakeDoc(args[0])
		if args and isinstance(args[0], str) and len(args) > 1 and isinstance(args[1], str):
			return fake_frappe._docs_store.get((args[0], args[1]))
		return FakeDoc(kwargs)

	def new_doc(doctype, *args, **kwargs):
		doc = FakeDoc({"doctype": doctype, "docstatus": 0, "__islocal": 1})
		doc.update(kwargs)
		return doc

	fake_frappe.get_doc = get_doc
	fake_frappe.new_doc = new_doc
	fake_frappe._docs_store = {}

	# Utils
	utils = ModuleType("frappe.utils")
	utils.flt = lambda v, precision=None: float(v or 0)
	utils.cint = lambda v: int(v or 0)
	utils.cstr = lambda v: str(v or "")
	fake_frappe.utils = utils
	fake_frappe.flt = utils.flt
	fake_frappe.cint = utils.cint
	fake_frappe.cstr = utils.cstr

	# Model mapper
	model_mod = ModuleType("frappe.model")
	mapper_mod = ModuleType("frappe.model.mapper")
	mapper_mod.get_mapped_doc = MagicMock()
	model_mod.mapper = mapper_mod
	fake_frappe.model = model_mod

	sys.modules["frappe"] = fake_frappe
	sys.modules["frappe.utils"] = utils
	sys.modules["frappe.model"] = model_mod
	sys.modules["frappe.model.mapper"] = mapper_mod

	# Stub erpnext modules
	erpnext = ModuleType("erpnext")
	sys.modules["erpnext"] = erpnext

	po_mod = ModuleType("erpnext.buying.doctype.purchase_order.purchase_order")
	po_mod.make_inter_company_sales_order = MagicMock()
	sys.modules["erpnext.buying.doctype.purchase_order.purchase_order"] = po_mod

	dn_mod = ModuleType("erpnext.stock.doctype.delivery_note.delivery_note")
	dn_mod.make_inter_company_purchase_receipt = MagicMock()
	sys.modules["erpnext.stock.doctype.delivery_note.delivery_note"] = dn_mod

	si_mod = ModuleType("erpnext.accounts.doctype.sales_invoice.sales_invoice")
	si_mod.get_inter_company_details = MagicMock()
	sys.modules["erpnext.accounts.doctype.sales_invoice.sales_invoice"] = si_mod


frappe = None
native_get_inter_company_details = None
native_make_sales_order = None
native_make_purchase_receipt = None


def tearDownModule():
	# Restore sys.modules so subsequent discovery or test runs are completely unaffected
	for mod in list(sys.modules):
		if mod not in _ORIGINAL_MODULES:
			del sys.modules[mod]


class TestMappingContracts(unittest.TestCase):
	@classmethod
	def tearDownClass(cls):
		pass

	def tearDown(self):
		for mod in list(sys.modules):
			if mod not in _ORIGINAL_MODULES:
				del sys.modules[mod]

	def setUp(self):
		_setup_fake_frappe()
		global frappe, native_get_inter_company_details, native_make_sales_order, native_make_purchase_receipt
		import frappe as _frappe
		frappe = _frappe
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
			get_inter_company_details,
		)
		from erpnext.buying.doctype.purchase_order.purchase_order import (
			make_inter_company_sales_order,
		)
		from erpnext.stock.doctype.delivery_note.delivery_note import (
			make_inter_company_purchase_receipt,
		)
		native_get_inter_company_details = get_inter_company_details
		native_make_sales_order = make_inter_company_sales_order
		native_make_purchase_receipt = make_inter_company_purchase_receipt
		frappe.db = frappe.FakeDB()
		frappe._docs_store = {}
		frappe.has_permission = lambda doctype, ptype="read", doc=None, user=None, throw=False: True
		frappe.db.get_all = lambda doctype, **kwargs: []
		frappe.get_list = lambda doctype, **kwargs: frappe.db.get_list(doctype, **kwargs)

		def default_get_value(doctype, filters, fieldname=None, as_dict=False):
			party_name = (
				filters
				if isinstance(filters, str)
				else (filters.get("name") if isinstance(filters, dict) else None)
			)
			if doctype == "Supplier" and (
				party_name in ("JURI Supplier", "Internal Supplier JURI")
				or (isinstance(party_name, tuple) and "JURI Supplier" in party_name)
			):
				if fieldname == "represents_company":
					return "JURI Company"
				if fieldname == "is_internal_supplier":
					return 1
				if as_dict or not fieldname:
					return FakeDoc(
						{
							"name": "JURI Supplier",
							"represents_company": "JURI Company",
							"is_internal_supplier": 1,
						}
					)
			if doctype == "Customer" and (
				party_name in ("Outlet Customer", "Internal Customer Outlet")
				or (isinstance(party_name, tuple) and "Outlet Customer" in party_name)
			):
				if fieldname == "represents_company":
					return "Outlet Company"
				if fieldname == "is_internal_customer":
					return 1
				if as_dict or not fieldname:
					return FakeDoc(
						{
							"name": "Outlet Customer",
							"represents_company": "Outlet Company",
							"is_internal_customer": 1,
						}
					)
			return None

		frappe.db.get_value = default_get_value
		frappe.db.exists = lambda doctype, filters: False

		def default_get_inter_company_details(doc, doctype):
			if doctype == "Purchase Order":
				return {"party": "Internal Customer Outlet", "company": "JURI Co"}
			elif doctype == "Delivery Note":
				return {"party": "Internal Supplier JURI", "company": "Outlet Co"}
			return {}

		native_get_inter_company_details.side_effect = default_get_inter_company_details
		native_make_sales_order.side_effect = None
		native_make_purchase_receipt.side_effect = None

		# Import under test fresh so it binds to current frappe
		import importlib
		if "intercompany_sync.mapping" in sys.modules:
			self.mapping = importlib.reload(sys.modules["intercompany_sync.mapping"])
		else:
			import intercompany_sync.mapping
			self.mapping = intercompany_sync.mapping

	def test_make_sales_order_happy_path(self):
		"""PO -> SO draft mapping: fixes native invoice_reference bug and preserves row IDs."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-2026-001",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"currency": "IDR",
				"items": [
					FakeDoc(
						{
							"name": "POI-001",
							"item_code": "ITEM-A",
							"qty": 10,
							"rate": 5000,
						}
					),
				],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# Native mapper returns a SO with the known native bug (inter_company_invoice_reference set instead of order ref)
		def fake_native_so(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Sales Order",
					"docstatus": 0,
					"company": "JURI Company",
					"customer": "Outlet Customer",
					"inter_company_invoice_reference": source_name,  # native bug
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-A",
								"qty": 10,
								"rate": 5000,
								"purchase_order": source_name,
								"purchase_order_item": "POI-001",
							}
						)
					],
				}
			)

		native_make_sales_order.side_effect = fake_native_so

		so = self.mapping.make_sales_order("PO-2026-001")
		self.assertEqual(so.doctype, "Sales Order")
		self.assertEqual(so.docstatus, 0)
		self.assertEqual(so.inter_company_order_reference, "PO-2026-001")
		self.assertIsNone(so.get("inter_company_invoice_reference"))
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].purchase_order, "PO-2026-001")
		self.assertEqual(so.items[0].purchase_order_item, "POI-001")

	def test_make_sales_order_duplicate_item_codes(self):
		"""Duplicate Item Codes must retain their respective distinct PO row IDs."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-2026-002",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [
					FakeDoc({"name": "POI-101", "item_code": "ITEM-DUP", "qty": 5}),
					FakeDoc({"name": "POI-102", "item_code": "ITEM-DUP", "qty": 15}),
				],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		def fake_native_so(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Sales Order",
					"docstatus": 0,
					"company": "JURI Company",
					"customer": "Outlet Customer",
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-DUP",
								"qty": 5,
								"purchase_order": source_name,
								"purchase_order_item": "POI-101",
							}
						),
						FakeDoc(
							{
								"item_code": "ITEM-DUP",
								"qty": 15,
								"purchase_order": source_name,
								"purchase_order_item": "POI-102",
							}
						),
					],
				}
			)

		native_make_sales_order.side_effect = fake_native_so

		so = self.mapping.make_sales_order("PO-2026-002")
		self.assertEqual(len(so.items), 2)
		self.assertEqual(so.items[0].purchase_order_item, "POI-101")
		self.assertEqual(so.items[1].purchase_order_item, "POI-102")

	def test_make_sales_order_rejected_if_source_canceled(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-CANCELED",
				"docstatus": 2,
				"company": "Outlet Company",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-CANCELED")

	def test_make_sales_order_rejected_if_source_is_return(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-RET",
				"docstatus": 1,
				"is_return": 1,
				"company": "Outlet Company",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-RET")

	def test_make_sales_order_rejected_if_so_already_exists(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-PAIRED",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		frappe.db.get_value = (
			lambda dt, filters, fieldname=None, as_dict=False: "SO-EXISTING"
			if dt == "Sales Order"
			else None
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-PAIRED")

	def test_make_sales_order_rejected_if_target_has_items(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-2026-003",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		target_doc = FakeDoc(
			{
				"doctype": "Sales Order",
				"items": [FakeDoc({"item_code": "EXISTING"})],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-2026-003", target_doc=target_doc)

	def test_make_sales_order_rejected_if_mixing_source(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-2026-004",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		target_doc = FakeDoc(
			{
				"doctype": "Sales Order",
				"inter_company_order_reference": "PO-OTHER",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-2026-004", target_doc=target_doc)

	def test_make_sales_order_permission_denied(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-NO-PERM",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		frappe.has_permission = lambda doctype, ptype="read", doc=None, user=None, throw=False: False
		with self.assertRaises(frappe.PermissionError):
			self.mapping.make_sales_order("PO-NO-PERM")

	def test_get_outstanding_dn_rows_helper(self):
		"""Outstanding helper calculates dispatched - (accepted + rejected) across submitted PRs."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-2026-001",
				"docstatus": 1,
				"items": [
					FakeDoc(
						{
							"name": "DNI-001",
							"item_code": "ITEM-1",
							"qty": 100,
							"stock_qty": 100,
							"conversion_factor": 1.0,
							"uom": "Nos",
							"stock_uom": "Nos",
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		# 60 accepted + 10 rejected received in submitted PR
		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				return [
					FakeDoc(
						{
							"qty": 60,
							"rejected_qty": 10,
							"conversion_factor": 1.0,
							"delivery_note_item": "DNI-001",
						}
					)
				]
			return []

		frappe.db.get_all = fake_get_all

		out = self.mapping.get_outstanding_dn_rows(dn)
		self.assertIn("DNI-001", out)
		# 100 dispatched - 70 total received = 30 outstanding
		self.assertEqual(out["DNI-001"]["stock_qty"], 30.0)
		self.assertEqual(out["DNI-001"]["qty"], 30.0)
		self.assertEqual(out["DNI-001"]["received_stock_qty"], 70.0)

	def test_make_purchase_receipt_partial(self):
		"""DN 100 -> PR 60 submitted -> make_purchase_receipt offers remaining 40."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-2026-002",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"conversion_rate": 1.0,
				"items": [
					FakeDoc(
						{
							"name": "DNI-002",
							"item_code": "ITEM-PARTIAL",
							"qty": 100,
							"stock_qty": 100,
							"rate": 1000,
							"conversion_factor": 1.0,
							"purchase_order": "PO-ORIG",
							"purchase_order_item": "POI-ORIG",
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		# 60 accepted already received
		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				return [
					FakeDoc(
						{
							"qty": 60,
							"rejected_qty": 0,
							"conversion_factor": 1.0,
							"delivery_note_item": "DNI-002",
						}
					)
				]
			return []

		frappe.db.get_all = fake_get_all

		# Native mapper would map 100 because Delivery Note Item.received_qty is 0
		def fake_native_pr(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Purchase Receipt",
					"docstatus": 0,
					"company": "Outlet Company",
					"supplier": "JURI Supplier",
					"inter_company_reference": source_name,
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-PARTIAL",
								"qty": 100,
								"stock_qty": 100,
								"rate": 1000,
								"conversion_factor": 1.0,
								"delivery_note_item": "DNI-002",
								"purchase_order": "PO-ORIG",
								"purchase_order_item": "POI-ORIG",
							}
						)
					],
				}
			)

		native_make_purchase_receipt.side_effect = fake_native_pr

		pr = self.mapping.make_purchase_receipt("DN-2026-002")
		self.assertEqual(len(pr.items), 1)
		self.assertEqual(pr.items[0].qty, 40.0)
		self.assertEqual(pr.items[0].stock_qty, 40.0)
		self.assertEqual(pr.items[0].amount, 40000.0)
		self.assertEqual(pr.items[0].delivery_note_item, "DNI-002")
		self.assertEqual(pr.items[0].purchase_order, "PO-ORIG")
		self.assertEqual(pr.items[0].purchase_order_item, "POI-ORIG")

	def test_make_purchase_receipt_rejected_if_fully_received(self):
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-FULL",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [
					FakeDoc(
						{
							"name": "DNI-FULL",
							"item_code": "ITEM-F",
							"qty": 50,
							"stock_qty": 50,
							"conversion_factor": 1.0,
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		frappe.db.get_all = lambda doctype, **kwargs: [
			FakeDoc(
				{
					"qty": 50,
					"rejected_qty": 0,
					"conversion_factor": 1.0,
					"delivery_note_item": "DNI-FULL",
				}
			)
		]

		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-FULL")

	def test_make_purchase_receipt_restores_po_row_reference_via_so_detail(self):
		"""Native mapper gap: if DN Item is missing PO link, restore it via SO Item row identity."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-GAP",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [
					FakeDoc(
						{
							"name": "DNI-GAP-1",
							"item_code": "ITEM-GAP",
							"qty": 20,
							"stock_qty": 20,
							"so_detail": "SOI-999",
							"purchase_order": None,
							"purchase_order_item": None,
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		# Mock SO Item lookup
		def fake_get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Customer":
				if fieldname == "represents_company":
					return "Outlet Company"
				if fieldname == "is_internal_customer":
					return 1
			if doctype == "Sales Order Item" and filters == "SOI-999":
				return FakeDoc(
					{
						"purchase_order": "PO-RECOVERED",
						"purchase_order_item": "POI-RECOVERED",
					}
				)
			if doctype == "Delivery Note Item" and filters == "DNI-GAP-1":
				return FakeDoc(
					{
						"purchase_order": None,
						"purchase_order_item": None,
						"so_detail": "SOI-999",
					}
				)
			return None

		frappe.db.get_value = fake_get_value

		def fake_native_pr(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Purchase Receipt",
					"docstatus": 0,
					"company": "Outlet Company",
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-GAP",
								"qty": 20,
								"stock_qty": 20,
								"delivery_note_item": "DNI-GAP-1",
								"purchase_order": None,
								"purchase_order_item": None,
							}
						)
					],
				}
			)

		native_make_purchase_receipt.side_effect = fake_native_pr

		pr = self.mapping.make_purchase_receipt("DN-GAP")
		self.assertEqual(pr.items[0].purchase_order, "PO-RECOVERED")
		self.assertEqual(pr.items[0].purchase_order_item, "POI-RECOVERED")

	def test_get_sources_po_filtering(self):
		"""get_sources for Purchase Order filters company/party, excludes paired/canceled/unpermitted."""
		pos = [
			FakeDoc(
				{
					"name": "PO-VALID",
					"company": "Outlet Co",
					"supplier": "Internal Supplier JURI",
					"docstatus": 1,
					"is_return": 0,
					"transaction_date": "2026-10-30",
					"grand_total": 50000,
				}
			),
			FakeDoc(
				{
					"name": "PO-ALREADY-PAIRED",
					"company": "Outlet Co",
					"supplier": "Internal Supplier JURI",
					"docstatus": 1,
					"is_return": 0,
					"transaction_date": "2026-10-30",
				}
			),
			FakeDoc(
				{
					"name": "PO-CANCELED",
					"company": "Outlet Co",
					"supplier": "Internal Supplier JURI",
					"docstatus": 2,
					"is_return": 0,
				}
			),
		]

		def fake_get_all(doctype, **kwargs):
			if doctype == "Supplier":
				res = [FakeDoc({"name": "Internal Supplier JURI"})]
			elif doctype == "Customer":
				res = [FakeDoc({"name": "Internal Customer Outlet"})]
			elif doctype == "Purchase Order":
				res = pos
			elif doctype == "Sales Order":
				res = [FakeDoc({"name": "SO-1", "inter_company_order_reference": "PO-ALREADY-PAIRED"})]
			else:
				res = []

			if kwargs.get("pluck"):
				pluck_col = kwargs["pluck"]
				return [r.get(pluck_col) for r in res if r.get(pluck_col)]
			return res

		frappe.db.get_all = fake_get_all

		def fake_get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Customer" and filters == "Internal Customer Outlet":
				if fieldname == "represents_company":
					return "Outlet Co"
				if fieldname == "is_internal_customer":
					return 1
				return "Outlet Co"
			return None

		frappe.db.get_value = fake_get_value

		res = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			target_party="Internal Customer Outlet",
		)
		names = [r["name"] for r in res]
		self.assertIn("PO-VALID", names)
		self.assertNotIn("PO-ALREADY-PAIRED", names)
		self.assertNotIn("PO-CANCELED", names)

	def test_get_sources_dn_filtering(self):
		"""get_sources for Delivery Note excludes fully-received DNs and respects target party."""
		dns = [
			FakeDoc(
				{
					"name": "DN-VALID",
					"company": "JURI Co",
					"customer": "Internal Customer Outlet",
					"docstatus": 1,
					"is_return": 0,
					"posting_date": "2026-10-30",
					"grand_total": 100000,
					"items": [
						FakeDoc(
							{
								"name": "DNI-V1",
								"item_code": "ITEM-1",
								"qty": 50,
								"stock_qty": 50,
								"conversion_factor": 1.0,
							}
						)
					],
				}
			),
			FakeDoc(
				{
					"name": "DN-FULLY-RECEIVED",
					"company": "JURI Co",
					"customer": "Internal Customer Outlet",
					"docstatus": 1,
					"is_return": 0,
					"posting_date": "2026-10-30",
					"items": [
						FakeDoc(
							{
								"name": "DNI-F1",
								"item_code": "ITEM-2",
								"qty": 10,
								"stock_qty": 10,
								"conversion_factor": 1.0,
							}
						)
					],
				}
			),
		]

		for d in dns:
			frappe._docs_store[("Delivery Note", d.name)] = d

		def fake_get_all(doctype, **kwargs):
			if doctype == "Customer":
				res = [FakeDoc({"name": "Internal Customer Outlet"})]
			elif doctype == "Supplier":
				res = [FakeDoc({"name": "Internal Supplier JURI"})]
			elif doctype == "Delivery Note":
				res = dns
			elif doctype == "Purchase Receipt Item":
				filters = kwargs.get("filters", {})
				parent_filter = filters.get("delivery_note_item", ("in", []))
				parent_items = parent_filter[1] if isinstance(parent_filter, tuple) else []
				if "DNI-F1" in parent_items:
					res = [
						FakeDoc(
							{
								"qty": 10,
								"rejected_qty": 0,
								"conversion_factor": 1.0,
								"delivery_note_item": "DNI-F1",
							}
						)
					]
				else:
					res = []
			else:
				res = []

			if kwargs.get("pluck"):
				pluck_col = kwargs["pluck"]
				return [r.get(pluck_col) for r in res if r.get(pluck_col)]
			return res

		frappe.db.get_all = fake_get_all

		def fake_get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Supplier" and filters == "Internal Supplier JURI":
				return "JURI Co"
			return None

		frappe.db.get_value = fake_get_value

		res = self.mapping.get_sources(
			source_doctype="Delivery Note",
			target_company="Outlet Co",
			target_party="Internal Supplier JURI",
		)
		names = [r["name"] for r in res]
		self.assertIn("DN-VALID", names)
		self.assertNotIn("DN-FULLY-RECEIVED", names)


	def test_get_sources_unsupported_doctype(self):
		with self.assertRaises(frappe.ValidationError):
			self.mapping.get_sources(
				source_doctype="Sales Order",
				target_company="JURI Co",
			)

	def test_get_sources_pagination_and_txt(self):
		pos = [
			FakeDoc(
				{
					"name": f"PO-SEARCH-{i:02d}",
					"company": "Outlet Co" if i % 2 == 0 else "Alpha Co",
					"supplier": "Internal Supplier JURI",
					"docstatus": 1,
					"is_return": 0,
					"transaction_date": "2026-10-30",
				}
			)
			for i in range(10)
		]

		frappe.db.get_all = (
			lambda dt, **kwargs: [FakeDoc({"name": "Internal Supplier JURI"})]
			if dt == "Supplier"
			else (pos if dt == "Purchase Order" else [])
		)

		# Search by txt "alpha"
		res_txt = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			txt="alpha",
		)
		self.assertTrue(all("Alpha Co" in r["company"] for r in res_txt))

		# Pagination: page_length 3, start 2
		res_page = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			start=2,
			page_length=3,
		)
		self.assertEqual(len(res_page), 3)
		self.assertEqual(res_page[0]["name"], "PO-SEARCH-02")

	def test_outstanding_dn_rows_with_exclude_pr_and_uom_conversion(self):
		"""UOM conversion: 10 boxes (conversion factor 10) = 100 stock units.

		If PR-1 received 3 boxes (30 stock units) and PR-CURRENT received 2 boxes (20 stock units),
		excluding PR-CURRENT yields 70 stock units (7 boxes) remaining.
		"""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-BOXES",
				"docstatus": 1,
				"items": [
					FakeDoc(
						{
							"name": "DNI-BOX-1",
							"item_code": "BOXED-ITEM",
							"qty": 10,
							"stock_qty": 100,
							"conversion_factor": 10.0,
							"uom": "Box",
							"stock_uom": "Nos",
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		def fake_get_all(doctype, **kwargs):
			if doctype == "Purchase Receipt Item":
				return [
					FakeDoc(
						{
							"parent": "PR-1",
							"delivery_note_item": "DNI-BOX-1",
							"qty": 3,  # 3 boxes = 30 nos
							"rejected_qty": 0,
							"conversion_factor": 10.0,
						}
					),
					FakeDoc(
						{
							"parent": "PR-CURRENT",
							"delivery_note_item": "DNI-BOX-1",
							"qty": 2,  # 2 boxes = 20 nos
							"rejected_qty": 0,
							"conversion_factor": 10.0,
						}
					),
				]
			return []

		frappe.db.get_all = fake_get_all

		out = self.mapping.get_outstanding_dn_rows(dn, exclude_pr="PR-CURRENT")
		# Dispatched 100 - 30 received (PR-1) = 70 stock_qty, which is 7 boxes
		self.assertEqual(out["DNI-BOX-1"]["stock_qty"], 70.0)
		self.assertEqual(out["DNI-BOX-1"]["qty"], 7.0)

	def test_make_purchase_receipt_rejected_if_target_has_items(self):
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-OCCUPIED",
				"docstatus": 1,
				"is_return": 0,
				"items": [FakeDoc({"name": "DNI-OCC", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn
		target_doc = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"items": [FakeDoc({"item_code": "EXISTING"})],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-OCCUPIED", target_doc=target_doc)

	def test_make_purchase_receipt_rejected_if_mixing_source(self):
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-MIX-1",
				"docstatus": 1,
				"is_return": 0,
				"items": [FakeDoc({"name": "DNI-M1", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn
		target_doc = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"inter_company_reference": "DN-OTHER",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-MIX-1", target_doc=target_doc)


	def test_make_sales_order_rejected_if_target_mismatched_company(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-MISMATCH-CO",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		frappe.db.get_value = (
			lambda dt, filters, fieldname=None, as_dict=False: "JURI Company"
			if dt == "Supplier"
			else None
		)

		target_doc = FakeDoc(
			{
				"doctype": "Sales Order",
				"company": "Wrong Company",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-MISMATCH-CO", target_doc=target_doc)

	def test_make_sales_order_rejected_if_target_mismatched_customer(self):
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-MISMATCH-CUST",
				"docstatus": 1,
				"is_return": 0,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		def fake_get_value(dt, filters, fieldname=None, as_dict=False):
			if dt == "Supplier":
				return "JURI Company"
			if dt == "Customer":
				return "Other Company"  # Does not represent Outlet Company
			return None

		frappe.db.get_value = fake_get_value

		target_doc = FakeDoc(
			{
				"doctype": "Sales Order",
				"company": "JURI Company",
				"customer": "Customer Other",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-MISMATCH-CUST", target_doc=target_doc)

	def test_make_purchase_receipt_rejected_if_target_mismatched_company(self):
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-MISMATCH-CO",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [FakeDoc({"name": "DNI-MC", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn
		frappe.db.get_value = (
			lambda dt, filters, fieldname=None, as_dict=False: "Outlet Company"
			if dt == "Customer"
			else None
		)

		target_doc = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"company": "Wrong Company",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-MISMATCH-CO", target_doc=target_doc)


	def test_po_schema_does_not_contain_is_return(self):
		"""Finding 1 regression: Purchase Order DocType has no is_return column in Frappe/ERPNext."""
		import json

		po_schema_path = _find_erpnext_schema("buying/doctype/purchase_order/purchase_order.json")
		if not po_schema_path or not po_schema_path.is_file():
			self.skipTest("ERPNext Purchase Order schema characterization skipped: schema file unavailable")

		with open(po_schema_path) as f:
			schema = json.load(f)
		fieldnames = {df.get("fieldname") for df in schema.get("fields", [])}
		self.assertNotIn(
			"is_return",
			fieldnames,
			"Purchase Order must not define is_return; queries must never filter on it.",
		)

	def test_make_purchase_receipt_synchronizes_totals_and_preserves_invoice_reference(self):
		"""Finding 2: physical receipt fields (received_qty, received_stock_qty) must be synchronized,

		taxes/totals recalculated, and native inter_company_invoice_reference preserved.
		"""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-COHERENT",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"conversion_rate": 1.0,
				"items": [
					FakeDoc(
						{
							"name": "DNI-COH",
							"item_code": "ITEM-COH",
							"qty": 100,
							"stock_qty": 200,
							"conversion_factor": 2.0,
							"rate": 1500,
							"purchase_order": "PO-1",
							"purchase_order_item": "POI-1",
						}
					)
				],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		# 50 stock units already received in previous PR
		frappe.db.get_all = lambda dt, **kwargs: [
			FakeDoc(
				{
					"qty": 25,
					"rejected_qty": 0,
					"conversion_factor": 2.0,
					"delivery_note_item": "DNI-COH",
				}
			)
		] if dt == "Purchase Receipt Item" else []

		calc_totals_called = []

		def fake_native_pr(source_name, target_doc=None):
			pr = FakeDoc(
				{
					"doctype": "Purchase Receipt",
					"docstatus": 0,
					"company": "Outlet Company",
					"supplier": "JURI Supplier",
					"inter_company_reference": source_name,
					"inter_company_invoice_reference": source_name,  # native PR reference
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-COH",
								"qty": 100,
								"stock_qty": 200,
								"received_qty": 100,
								"received_stock_qty": 200,
								"rejected_qty": 0,
								"rate": 1500,
								"amount": 150000,
								"conversion_factor": 2.0,
								"delivery_note_item": "DNI-COH",
								"purchase_order": "PO-1",
								"purchase_order_item": "POI-1",
							}
						)
					],
				}
			)
			pr.calculate_taxes_and_totals = lambda: calc_totals_called.append(True)
			return pr

		native_make_purchase_receipt.side_effect = fake_native_pr

		pr = self.mapping.make_purchase_receipt("DN-COHERENT")
		item = pr.items[0]
		# Dispatched 200 - 50 received = 150 stock_qty -> 75 qty (at cf=2)
		self.assertEqual(item.stock_qty, 150.0)
		self.assertEqual(item.qty, 75.0)
		self.assertEqual(item.received_qty, 75.0)
		self.assertEqual(item.received_stock_qty, 150.0)
		self.assertEqual(item.amount, 112500.0)
		self.assertEqual(len(calc_totals_called), 1, "calculate_taxes_and_totals must be rerun")
		self.assertEqual(
			pr.inter_company_invoice_reference,
			"DN-COHERENT",
			"Native inter_company_invoice_reference on PR must be preserved",
		)

	def test_make_sales_order_allows_same_saved_target_doc(self):
		"""Finding 3: duplicate SO check must exclude the same saved draft target doc."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-SAME-TARGET",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-ST", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# The DB already has SO-DRAFT-1 with inter_company_order_reference = "PO-SAME-TARGET"
		def fake_get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Supplier":
				return "JURI Company"
			if doctype == "Sales Order" and isinstance(filters, dict) and filters.get("inter_company_order_reference") == "PO-SAME-TARGET":
				# If filter excludes SO-DRAFT-1, return None!
				name_cond = filters.get("name")
				if isinstance(name_cond, tuple) and name_cond[0] == "!=" and name_cond[1] == "SO-DRAFT-1":
					return None
				return "SO-DRAFT-1"
			return None

		frappe.db.get_value = fake_get_value

		def fake_native_so(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Sales Order",
					"name": "SO-DRAFT-1",
					"docstatus": 0,
					"company": "JURI Company",
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-1",
								"qty": 10,
								"purchase_order": source_name,
								"purchase_order_item": "POI-ST",
							}
						)
					],
				}
			)

		native_make_sales_order.side_effect = fake_native_so

		# Target doc is the SAME draft Sales Order
		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-DRAFT-1",
				"docstatus": 0,
				"company": "JURI Company",
				"inter_company_order_reference": "PO-SAME-TARGET",
				"items": [],
			}
		)
		frappe._docs_store[("Sales Order", "SO-DRAFT-1")] = target
		frappe.db.exists = lambda dt, name: (dt == "Sales Order" and name == "SO-DRAFT-1")
		so = self.mapping.make_sales_order("PO-SAME-TARGET", target_doc=target)
		self.assertEqual(so.name, "SO-DRAFT-1")

	def test_make_sales_order_fails_closed_on_external_customer(self):
		"""Finding 4: external customer lacking represents_company must be rejected."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-EXT-CUST",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		def fake_get_value(dt, filters, fieldname=None, as_dict=False):
			if dt == "Supplier":
				return "JURI Company"
			if dt == "Customer":
				# External customer: represents_company is None
				return None
			return None

		frappe.db.get_value = fake_get_value

		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"company": "JURI Company",
				"customer": "External Customer Inc",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-EXT-CUST", target_doc=target)

	def test_make_sales_order_rejects_malformed_native_mapper_missing_row_id(self):
		"""Finding 5: broken native mapper returning missing or unknown purchase_order_item must be rejected."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-BROKEN-MAP",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-VALID", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po
		frappe.db.get_value = lambda dt, *a, **k: "JURI Company" if dt == "Supplier" else None

		# Intentionally broken native mapper omits purchase_order_item or returns an unknown ID
		def fake_broken_so(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Sales Order",
					"docstatus": 0,
					"company": "JURI Company",
					"items": [
						FakeDoc(
							{
								"item_code": "ITEM-1",
								"qty": 10,
								"purchase_order": source_name,
								"purchase_order_item": None,  # Broken!
							}
						)
					],
				}
			)

		native_make_sales_order.side_effect = fake_broken_so

		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-BROKEN-MAP")

	def test_make_sales_order_does_not_leak_so_name_if_unreadable(self):
		"""Finding 6: duplicate error message must not disclose secret SO name if user has no read perm."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-SECRET-SO",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		frappe.db.get_value = lambda dt, filters, *a, **k: "SO-SECRET-999" if dt == "Sales Order" else "JURI Company"

		# User cannot read SO-SECRET-999
		def fake_has_perm(doctype, ptype="read", doc=None, user=None, throw=False):
			return not (doctype == "Sales Order" and doc == "SO-SECRET-999")

		frappe.has_permission = fake_has_perm

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.mapping.make_sales_order("PO-SECRET-SO")
		self.assertNotIn("SO-SECRET-999", str(ctx.exception), "Unreadable SO name must not be disclosed")

	def test_get_sources_uses_permission_aware_get_list_with_chunked_scanning(self):
		"""Finding 7: get_sources uses get_list with clamped page params and chunk scanning skipping ineligibles."""
		# Simulate 5 ineligible DNs (fully received) followed by 2 eligible DNs
		dns = []
		for i in range(5):
			dns.append(
				FakeDoc(
					{
						"name": f"DN-INELIGIBLE-{i}",
						"company": "JURI Co",
						"customer": "Internal Customer Outlet",
						"docstatus": 1,
						"is_return": 0,
						"posting_date": "2026-10-30",
						"items": [FakeDoc({"name": f"DNI-INEL-{i}", "stock_qty": 10, "qty": 10})],
					}
				)
			)
		for i in range(2):
			dns.append(
				FakeDoc(
					{
						"name": f"DN-ELIGIBLE-{i}",
						"company": "JURI Co",
						"customer": "Internal Customer Outlet",
						"docstatus": 1,
						"is_return": 0,
						"posting_date": "2026-10-30",
						"items": [FakeDoc({"name": f"DNI-ELIG-{i}", "stock_qty": 20, "qty": 20})],
					}
				)
			)

		for d in dns:
			frappe._docs_store[("Delivery Note", d.name)] = d

		get_list_calls = []

		def fake_get_list(doctype, **kwargs):
			get_list_calls.append(kwargs)
			if doctype == "Delivery Note":
				start = kwargs.get("start", 0)
				page_len = kwargs.get("page_length", 20)
				return dns[start : start + page_len]
			return []

		frappe.get_list = fake_get_list

		def fake_get_all(doctype, **kwargs):
			if doctype == "Customer":
				return ["Internal Customer Outlet"] if kwargs.get("pluck") else [FakeDoc({"name": "Internal Customer Outlet"})]
			if doctype == "Purchase Receipt Item":
				# All DNI-INEL rows are fully received
				filters = kwargs.get("filters", {})
				parent_filter = filters.get("delivery_note_item", ("in", []))
				items = parent_filter[1] if isinstance(parent_filter, tuple) else []
				return [
					FakeDoc(
						{
							"qty": 10,
							"rejected_qty": 0,
							"conversion_factor": 1.0,
							"delivery_note_item": dni,
						}
					)
					for dni in items
					if "INEL" in dni
				]
			return []

		frappe.db.get_all = fake_get_all

		res = self.mapping.get_sources(
			source_doctype="Delivery Note",
			target_company="Outlet Co",
			start=0,
			page_length=2,
		)
		self.assertEqual(len(res), 2)
		self.assertEqual(res[0]["name"], "DN-ELIGIBLE-0")
		self.assertEqual(res[1]["name"], "DN-ELIGIBLE-1")
		self.assertTrue(len(get_list_calls) > 0, "frappe.get_list must be used for permission safety")


	def test_get_sources_po_does_not_filter_is_return(self):
		"""Finding 1: verify get_sources for Purchase Order does not pass is_return in filters."""
		filters_passed = []

		def fake_get_list(doctype, **kwargs):
			filters_passed.append(kwargs.get("filters"))
			return []

		frappe.get_list = fake_get_list
		frappe.db.get_all = (
			lambda dt, **k: ["Internal Supplier JURI"]
			if dt == "Supplier" and k.get("pluck")
			else []
		)

		self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
		)
		self.assertTrue(len(filters_passed) > 0)
		for f in filters_passed:
			if isinstance(f, list):
				fieldnames = [c[0] for c in f]
				self.assertNotIn("is_return", fieldnames, "PO query must not filter is_return")

	def test_make_purchase_receipt_fails_closed_on_external_supplier(self):
		"""Finding 4: external supplier lacking represents_company must be rejected."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-EXT-SUPP",
				"docstatus": 1,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [FakeDoc({"name": "DNI-ES", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		def fake_get_value(dt, filters, fieldname=None, as_dict=False):
			if dt == "Customer":
				return "Outlet Company"
			if dt == "Supplier":
				# External supplier: represents_company is None
				return None
			return None

		frappe.db.get_value = fake_get_value

		target = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"company": "Outlet Company",
				"supplier": "External Supplier Ltd",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-EXT-SUPP", target_doc=target)

	def test_get_sources_fails_closed_on_external_party(self):
		"""Finding 4: get_sources returns empty list when target_party is not an internal party."""
		frappe.db.get_value = lambda dt, *a, **k: None  # No internal representation

		res_po = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			target_party="External Customer",
		)
		self.assertEqual(res_po, [])

		res_dn = self.mapping.get_sources(
			source_doctype="Delivery Note",
			target_company="Outlet Co",
			target_party="External Supplier",
		)
		self.assertEqual(res_dn, [])


	def test_make_sales_order_rejects_spoofed_submitted_target_doc(self):
		"""Finding 3 (round 2): target_doc with persisted submitted status (docstatus=1) must be rejected."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-SPOOF-1",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-SP1", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# Persisted SO in DB is submitted (docstatus=1)
		persisted_so = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-PERSISTED-SUBMITTED",
				"docstatus": 1,
				"company": "JURI Company",
				"items": [],
			}
		)
		frappe._docs_store[("Sales Order", "SO-PERSISTED-SUBMITTED")] = persisted_so
		frappe.db.exists = lambda dt, name: (dt == "Sales Order" and name == "SO-PERSISTED-SUBMITTED")

		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-PERSISTED-SUBMITTED",
				"company": "JURI Company",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-SPOOF-1", target_doc=target)

	def test_make_sales_order_rejects_unreadable_or_unwritable_persisted_target_doc(self):
		"""Finding 3 (round 2): persisted target doc requires read and write permission."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-PERM-TGT",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-PT", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		persisted_so = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-NO-WRITE",
				"docstatus": 0,
				"company": "JURI Company",
				"items": [],
			}
		)
		frappe._docs_store[("Sales Order", "SO-NO-WRITE")] = persisted_so
		frappe.db.exists = lambda dt, name: (dt == "Sales Order" and name == "SO-NO-WRITE")

		# User cannot write to this SO
		def fake_has_perm(doctype, ptype="read", doc=None, user=None, throw=False):
			return not (doctype == "Sales Order" and ptype == "write")

		frappe.has_permission = fake_has_perm

		target = FakeDoc({"doctype": "Sales Order", "name": "SO-NO-WRITE", "company": "JURI Company", "items": []})
		with self.assertRaises(frappe.PermissionError):
			self.mapping.make_sales_order("PO-PERM-TGT", target_doc=target)

	def test_make_sales_order_rejects_islocal_spoofed_duplicate_name(self):
		"""Finding 3 (round 2): new local doc (__islocal=1) cannot exclude existing paired name."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-LOCAL-SPOOF",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-LS", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# DB has active SO with name "SO-EXISTING"
		frappe.db.get_value = lambda dt, filters, *a, **k: "SO-EXISTING" if dt == "Sales Order" else "JURI Company"

		# Client sends new local doc spoofing the existing name
		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-EXISTING",
				"__islocal": 1,
				"company": "JURI Company",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-LOCAL-SPOOF", target_doc=target)

	def test_make_sales_order_rejects_mismatched_target_party_when_multiple_internal_parties_exist(self):
		"""Finding 4 (round 2): reject when native mapper resolved customer differs from supplied target.customer."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-MULTI-PARTY",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-MP", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# Native mapper resolves Customer A
		def fake_native_so(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Sales Order",
					"docstatus": 0,
					"company": "JURI Company",
					"customer": "Customer Division A",
					"items": [
						FakeDoc({"item_code": "ITEM-1", "qty": 10, "purchase_order": source_name, "purchase_order_item": "POI-MP"})
					],
				}
			)

		native_make_sales_order.side_effect = fake_native_so

		# Target doc requested Customer Division B (even if both represent Outlet Company)
		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"company": "JURI Company",
				"customer": "Customer Division B",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-MULTI-PARTY", target_doc=target)

	def test_make_purchase_receipt_rejects_mismatched_target_party_when_multiple_internal_parties_exist(self):
		"""Finding 4 (round 2): reject when native mapper resolved supplier differs from supplied target.supplier."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-MULTI-PARTY",
				"docstatus": 1,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [FakeDoc({"name": "DNI-MP", "item_code": "ITEM-1", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		# Native mapper resolves Supplier A
		def fake_native_pr(source_name, target_doc=None):
			return FakeDoc(
				{
					"doctype": "Purchase Receipt",
					"docstatus": 0,
					"company": "Outlet Company",
					"supplier": "Supplier Branch A",
					"items": [
						FakeDoc({"delivery_note_item": "DNI-MP", "item_code": "ITEM-1", "qty": 10, "stock_qty": 10})
					],
				}
			)

		native_make_purchase_receipt.side_effect = fake_native_pr

		# Target doc requested Supplier Branch B
		target = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"company": "Outlet Company",
				"supplier": "Supplier Branch B",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-MULTI-PARTY", target_doc=target)

	def test_get_sources_uses_native_get_inter_company_details_resolver(self):
		"""Finding 4 (round 2): get_sources filters candidate using native get_inter_company_details resolver."""
		po1 = FakeDoc({"name": "PO-CORRECT-PARTY", "company": "Outlet Co", "supplier": "JURI Supp 1", "docstatus": 1})
		po2 = FakeDoc({"name": "PO-OTHER-PARTY", "company": "Outlet Co", "supplier": "JURI Supp 2", "docstatus": 1})

		frappe.db.get_all = (
			lambda dt, **k: ["JURI Supp 1", "JURI Supp 2"]
			if dt == "Supplier" and k.get("pluck")
			else []
		)
		frappe.get_list = (
			lambda dt, **k: [po1, po2]
			if dt == "Purchase Order"
			else []
		)

		def fake_resolver(doc, doctype):
			if doc.name == "PO-CORRECT-PARTY":
				return {"party": "Customer Target Division", "company": "JURI Co"}
			return {"party": "Customer Other Division", "company": "JURI Co"}

		native_get_inter_company_details.side_effect = fake_resolver

		res = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			target_party="Customer Target Division",
		)
		names = [r["name"] for r in res]
		self.assertIn("PO-CORRECT-PARTY", names)
		self.assertNotIn("PO-OTHER-PARTY", names)

	def test_get_sources_po_pair_check_works_without_user_so_read_permission(self):
		"""Finding 7 (round 2): paired SO check uses internal db query so users without SO read perm don't see paired POs."""
		po1 = FakeDoc({"name": "PO-UNPAIRED", "company": "Outlet Co", "supplier": "JURI Supp", "docstatus": 1})
		po2 = FakeDoc({"name": "PO-ALREADY-PAIRED", "company": "Outlet Co", "supplier": "JURI Supp", "docstatus": 1})

		frappe.get_list = (
			lambda dt, **k: [po1, po2]
			if dt == "Purchase Order"
			else []
		)

		def fake_has_perm(doctype, ptype="read", doc=None, user=None, throw=False):
			# User has NO permission on Sales Order
			if doctype == "Sales Order":
				if throw:
					raise frappe.PermissionError("No read permission for Sales Order")
				return False
			return True

		frappe.has_permission = fake_has_perm

		# Internal db.get_all identifies the paired SO
		def fake_db_get_all(doctype, **kwargs):
			if doctype == "Supplier":
				return ["JURI Supp"]
			if doctype == "Sales Order":
				return ["PO-ALREADY-PAIRED"]
			return []

		frappe.db.get_all = fake_db_get_all

		res = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
		)
		names = [r["name"] for r in res]
		self.assertIn("PO-UNPAIRED", names)
		self.assertNotIn("PO-ALREADY-PAIRED", names)

	def test_get_sources_warns_refine_search_when_cap500_reached(self):
		"""Finding 7 (round 2): msgprint warns to refine search when 500-scan cap is hit."""
		# 500 ineligible POs (already paired)
		ineligible_pos = [
			FakeDoc({"name": f"PO-INEL-{i}", "company": "Outlet Co", "supplier": "JURI Supp", "docstatus": 1})
			for i in range(500)
		]

		frappe.db.get_all = (
			lambda dt, **k: ["JURI Supp"]
			if dt == "Supplier" and k.get("pluck")
			else ([p.name for p in ineligible_pos] if dt == "Sales Order" else [])
		)

		frappe.get_list = (
			lambda dt, **k: ineligible_pos[k.get("start", 0) : k.get("start", 0) + k.get("page_length", 20)]
			if dt == "Purchase Order"
			else []
		)

		msg_calls = []
		frappe.msgprint = lambda msg, *a, **k: msg_calls.append(msg)

		res = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			start=0,
			page_length=20,
		)
		self.assertEqual(len(res), 0)
		self.assertTrue(len(msg_calls) > 0, "Warning to refine search must be emitted when 500 cap is reached")
		self.assertTrue(any("500" in str(m) for m in msg_calls))


	def test_validate_target_doc_rejects_occupied_persisted_draft_even_if_payload_items_empty(self):
		"""Fix 1 (round 3): reject target if persisted draft in DB has items, even if payload.items is empty."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-PERSISTED-ITEMS",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-PI", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		# In DB, SO-PERSISTED-1 has items already
		persisted_so = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-PERSISTED-1",
				"docstatus": 0,
				"company": "JURI Company",
				"items": [FakeDoc({"item_code": "ALREADY-HERE", "qty": 5})],
			}
		)
		frappe._docs_store[("Sales Order", "SO-PERSISTED-1")] = persisted_so
		frappe.db.exists = lambda dt, name: (dt == "Sales Order" and name == "SO-PERSISTED-1")

		# Client sends target with empty items list
		payload_target = FakeDoc(
			{
				"doctype": "Sales Order",
				"name": "SO-PERSISTED-1",
				"company": "JURI Company",
				"items": [],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-PERSISTED-ITEMS", target_doc=payload_target)

	def test_make_sales_order_captures_supplied_party_before_mapper_mutates_target(self):
		"""Fix 2 (round 3): capture supplied target.customer scalar before native mapper mutates target_doc."""
		po = FakeDoc(
			{
				"doctype": "Purchase Order",
				"name": "PO-MUTATE-SO",
				"docstatus": 1,
				"company": "Outlet Company",
				"supplier": "JURI Supplier",
				"items": [FakeDoc({"name": "POI-MSO", "item_code": "ITEM-1", "qty": 10})],
			}
		)
		frappe._docs_store[("Purchase Order", po.name)] = po

		target = FakeDoc(
			{
				"doctype": "Sales Order",
				"company": "JURI Company",
				"customer": "Customer Requested",  # user chose this on form
				"items": [],
			}
		)

		# Native mapper MUTATES the same target object to "Customer Native Mapped" and returns it
		def fake_mutating_native_so(source_name, target_doc=None):
			self.assertIs(target_doc, target, "Native mapper contract mutates the same target object")
			target_doc.customer = "Customer Native Mapped"
			target_doc.items = [
				FakeDoc({"item_code": "ITEM-1", "qty": 10, "purchase_order": source_name, "purchase_order_item": "POI-MSO"})
			]
			return target_doc

		native_make_sales_order.side_effect = fake_mutating_native_so

		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_sales_order("PO-MUTATE-SO", target_doc=target)

	def test_make_purchase_receipt_captures_supplied_party_before_mapper_mutates_target(self):
		"""Fix 2 (round 3): capture supplied target.supplier scalar before native mapper mutates target_doc."""
		dn = FakeDoc(
			{
				"doctype": "Delivery Note",
				"name": "DN-MUTATE-PR",
				"docstatus": 1,
				"is_return": 0,
				"company": "JURI Company",
				"customer": "Outlet Customer",
				"items": [FakeDoc({"name": "DNI-MPR", "item_code": "ITEM-1", "qty": 10, "stock_qty": 10})],
			}
		)
		frappe._docs_store[("Delivery Note", dn.name)] = dn

		target = FakeDoc(
			{
				"doctype": "Purchase Receipt",
				"company": "Outlet Company",
				"supplier": "Supplier Requested",
				"items": [],
			}
		)

		def fake_mutating_native_pr(source_name, target_doc=None):
			self.assertIs(target_doc, target, "Native mapper contract mutates the same target object")
			target_doc.supplier = "Supplier Native Mapped"
			target_doc.items = [
				FakeDoc({"delivery_note_item": "DNI-MPR", "item_code": "ITEM-1", "qty": 10, "stock_qty": 10})
			]
			return target_doc

		native_make_purchase_receipt.side_effect = fake_mutating_native_pr

		with self.assertRaises(frappe.ValidationError):
			self.mapping.make_purchase_receipt("DN-MUTATE-PR", target_doc=target)

	def test_get_sources_resolves_second_party_based_on_address(self):
		"""Fix 3 (round 3 & 4): projected fields include shipping_address (and DN company_address) for party resolution.
		PO does not define company_address, so only shipping_address is projected on PO.
		"""
		po_addr1 = FakeDoc(
			{
				"name": "PO-ADDR-1",
				"company": "Outlet Co",
				"supplier": "JURI Supp",
				"shipping_address": "ADDR-SHIP-1",
				"docstatus": 1,
			}
		)
		po_addr2 = FakeDoc(
			{
				"name": "PO-ADDR-2",
				"company": "Outlet Co",
				"supplier": "JURI Supp",
				"shipping_address": "ADDR-SHIP-2",
				"docstatus": 1,
			}
		)

		# Ensure get_list preserves projected fields
		def fake_get_list(doctype, **kwargs):
			if doctype == "Purchase Order":
				fields = kwargs.get("fields", [])
				self.assertNotIn("company_address", fields, "get_list must not project company_address on PO")
				self.assertIn("shipping_address", fields, "get_list must project shipping_address")
				return [po_addr1, po_addr2]
			return []

		frappe.get_list = fake_get_list

		# get_inter_company_details uses address to resolve party
		def fake_address_resolver(doc, doctype):
			if doc.get("shipping_address") == "ADDR-SHIP-2" or doc.get("company_address") == "ADDR-BRANCH-2":
				return {"party": "Customer Branch 2", "company": "JURI Co"}
			return {"party": "Customer Branch 1", "company": "JURI Co"}

		native_get_inter_company_details.side_effect = fake_address_resolver

		res = self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
			target_party="Customer Branch 2",
		)
		names = [r["name"] for r in res]
		self.assertIn("PO-ADDR-2", names)
		self.assertNotIn("PO-ADDR-1", names)

	def test_get_sources_po_projection_schema_regression(self):
		"""Round 4 regression: Purchase Order projected fields must exist in purchase_order.json or standard fields.

		In particular, PO does not have company_address (unlike Delivery Note).
		"""
		import json

		po_schema_path = _find_erpnext_schema("buying/doctype/purchase_order/purchase_order.json")
		if not po_schema_path or not po_schema_path.is_file():
			self.skipTest("ERPNext Purchase Order schema characterization skipped: schema file unavailable")

		with open(po_schema_path) as f:
			schema = json.load(f)

		schema_fieldnames = {df.get("fieldname") for df in schema.get("fields", []) if df.get("fieldname")}
		# Frappe standard DocType fields automatically present in DB table
		standard_fields = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
		allowed_fields = schema_fieldnames | standard_fields

		# Confirm schema fact: company_address is NOT in PO schema
		self.assertNotIn("company_address", schema_fieldnames, "Purchase Order schema must not contain company_address")

		captured_fields = []

		def fake_get_list(doctype, **kwargs):
			if doctype == "Purchase Order":
				captured_fields.extend(kwargs.get("fields", []))
			return []

		frappe.get_list = fake_get_list

		self.mapping.get_sources(
			source_doctype="Purchase Order",
			target_company="JURI Co",
		)

		self.assertTrue(captured_fields, "get_sources must query fields via frappe.get_list")
		self.assertNotIn(
			"company_address",
			captured_fields,
			"Purchase Order projection must not contain 'company_address' (nonexistent PO column)",
		)
		self.assertIn(
			"shipping_address",
			captured_fields,
			"Purchase Order projection must retain 'shipping_address' for address resolution",
		)
		for field in captured_fields:
			self.assertIn(
				field,
				allowed_fields,
				f"Field '{field}' projected for Purchase Order does not exist in PO schema or standard fields",
			)

	def test_schema_characterization_skips_when_unavailable(self):
		"""Finding 5: External schema characterization skips explicitly when file is unavailable rather than failing."""
		res = _find_erpnext_schema("nonexistent/path/for/test.json")
		self.assertIsNone(res)

		orig_find = globals().get("_find_erpnext_schema")
		try:
			globals()["_find_erpnext_schema"] = lambda subpath: None
			with self.assertRaises(unittest.SkipTest):
				self.test_po_schema_does_not_contain_is_return()
			with self.assertRaises(unittest.SkipTest):
				self.test_get_sources_po_projection_schema_regression()
		finally:
			globals()["_find_erpnext_schema"] = orig_find


if __name__ == "__main__":
	unittest.main()
