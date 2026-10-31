"""Isolated contract tests for Task 4: Connections and Dashboard Overrides.

Verifies:
- Pure dashboard enrichers: purchase_order(data), sales_order(data), delivery_note(data), purchase_receipt(data)
- Data.method custom get_open_count and get_related_documents endpoints
- Exact row lineage across PO <-> SO <-> DN <-> PR in all directions
- Duplicate child items yield distinct names
- Multiple partial receipts (1:N DN -> PRs) all discovered
- Canceled documents (docstatus == 2) excluded from counts and lists
- Hidden bridge: intermediate SO/DN unreadable by user never leaks into response or errors; readable PR reached
- Target DocType without read permission returns empty list without error
- Multi-order SO with separate POs preserves row lineage without sibling overreach
- Source read permission check raises PermissionError when unauthorized
- Unsupported DocType raises ValidationError
- get_open_count validates requested items against dashboard allowed transactions
- Unrelated native envelope (external links, internal links, timeline data) preserved

Runnable without host Frappe runtime using lightweight stubs with scoped sys.modules teardown.
"""

import sys
import unittest
from types import ModuleType
from unittest.mock import MagicMock

_ORIGINAL_MODULES = dict(sys.modules)


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


class FakeDoc(FakeFrappeDict):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		if "items" not in self:
			self["items"] = []

	def get(self, key, default=None):
		return super().get(key, default)


class PermissionError(Exception):
	pass


class ValidationError(Exception):
	pass


def _setup_fake_frappe():
	fake_frappe = ModuleType("frappe")
	fake_frappe._is_fake = True
	fake_frappe._dict = FakeFrappeDict
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

	# In-memory document store: (doctype, name) -> FakeDoc
	docs_store = {}
	# Permissions store: (doctype, perm_type, user_or_doc) -> bool
	# Default: all read permissions are True unless overridden
	perms_store = {}
	doctype_read_perms = {
		"Purchase Order": True,
		"Sales Order": True,
		"Delivery Note": True,
		"Purchase Receipt": True,
	}

	class FakeDB:
		def __init__(self):
			self.docs = docs_store

		def exists(self, doctype, name):
			return (doctype, name) in self.docs

		def get_value(self, doctype, filters, fieldname=None, as_dict=False):
			if isinstance(filters, str):
				doc = self.docs.get((doctype, filters))
				if not doc:
					return None
				if isinstance(fieldname, list):
					res = {f: doc.get(f) for f in fieldname}
					return FakeFrappeDict(res) if as_dict else [doc.get(f) for f in fieldname]
				return doc.get(fieldname)
			elif isinstance(filters, dict):
				for (dt, _), d in self.docs.items():
					if dt != doctype:
						continue
					match = True
					for k, v in filters.items():
						if isinstance(v, tuple) and v[0] == "<":
							if not (d.get(k) is not None and d.get(k) < v[1]):
								match = False
								break
						elif d.get(k) != v:
							match = False
							break
					if match:
						if isinstance(fieldname, list):
							res = {f: d.get(f) for f in fieldname}
							return FakeFrappeDict(res) if as_dict else [d.get(f) for f in fieldname]
						return d.get(fieldname)
			return None

		def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None, **kwargs):
			results = []
			# Check if querying child table
			is_child = doctype.endswith("Item")
			items_pool = []
			if is_child:
				parent_dt = doctype[:-5]  # e.g. "Sales Order" from "Sales Order Item"
				for (dt, p_name), p_doc in self.docs.items():
					if dt == parent_dt:
						for item in p_doc.get("items", []):
							row = dict(item)
							row["parent"] = p_name
							row["parenttype"] = parent_dt
							row["docstatus"] = p_doc.get("docstatus", 0)
							items_pool.append(row)
			else:
				for (dt, name), doc in self.docs.items():
					if dt == doctype:
						row = dict(doc)
						row["name"] = name
						items_pool.append(row)

			for row in items_pool:
				match = True
				if filters:
					for k, cond in filters.items():
						val = row.get(k)
						if isinstance(cond, tuple):
							op, target = cond
							if (op == "<" and not (val is not None and val < target)) or (op == "in" and val not in target) or (op == "!=" and val == target):
								match = False
								break
						elif isinstance(cond, list):
							if val not in cond:
								match = False
								break
						else:
							if val != cond:
								match = False
								break
				if match:
					if pluck:
						results.append(row.get(pluck))
					elif fields:
						results.append(FakeFrappeDict({f: row.get(f) for f in fields}))
					else:
						results.append(FakeFrappeDict(row))
			return results

	fake_db = FakeDB()
	fake_frappe.db = fake_db

	def get_doc(doctype, name=None, **kwargs):
		if isinstance(doctype, dict):
			return FakeDoc(doctype)
		if name is None:
			return FakeDoc(doctype=doctype, **kwargs)
		doc = docs_store.get((doctype, name))
		if not doc:
			raise fake_frappe.ValidationError(f"{doctype} {name} not found")
		return doc

	fake_frappe.get_doc = get_doc

	def get_all(doctype, **kwargs):
		return fake_db.get_all(doctype, **kwargs)

	fake_frappe.get_all = get_all

	def get_list(doctype, **kwargs):
		# get_list respects permissions: if doctype read perm is False, returns empty
		if not doctype_read_perms.get(doctype, True):
			return []
		raw = fake_db.get_all(doctype, **kwargs)
		# Filter each doc by has_permission
		res = []
		for item in raw:
			doc_name = item.get("name") if isinstance(item, dict) else item
			if has_permission(doctype, "read", doc=doc_name):
				res.append(item)
		return res

	fake_frappe.get_list = get_list

	def has_permission(doctype, ptype="read", doc=None, user=None):
		# Check doctype-level permission
		if ptype == "read" and not doctype_read_perms.get(doctype, True):
			return False
		doc_name = doc.get("name") if isinstance(doc, dict) else doc
		if (doctype, ptype, doc_name) in perms_store:
			return perms_store[(doctype, ptype, doc_name)]
		return True

	fake_frappe.has_permission = has_permission

	# Meta stub
	class FakeMeta:
		def __init__(self, doctype):
			self.doctype = doctype

		def get_dashboard_data(self):
			# Return standard transactions
			if self.doctype == "Purchase Order":
				return FakeFrappeDict({
					"fieldname": "purchase_order",
					"transactions": [
						{"label": "Related", "items": ["Purchase Receipt", "Purchase Invoice", "Sales Order"]},
						{"label": "Inter Company", "items": ["Delivery Note"]},
						{"label": "Payment", "items": ["Payment Entry"]},
					],
				})
			elif self.doctype == "Sales Order":
				return FakeFrappeDict({
					"fieldname": "sales_order",
					"transactions": [
						{"label": "Fulfillment", "items": ["Delivery Note", "Sales Invoice"]},
						{"label": "Purchasing", "items": ["Purchase Order"]},
						{"label": "Inter Company", "items": ["Purchase Receipt"]},
					],
				})
			elif self.doctype == "Delivery Note":
				return FakeFrappeDict({
					"fieldname": "delivery_note",
					"transactions": [
						{"label": "Reference", "items": ["Sales Order"]},
						{"label": "Internal Transfer", "items": ["Purchase Order", "Purchase Receipt"]},
						{"label": "Related", "items": ["Sales Invoice"]},
					],
				})
			elif self.doctype == "Purchase Receipt":
				return FakeFrappeDict({
					"fieldname": "purchase_receipt_no",
					"transactions": [
						{"label": "Reference", "items": ["Purchase Order"]},
						{"label": "Inter Company", "items": ["Sales Order", "Delivery Note"]},
						{"label": "Related", "items": ["Purchase Invoice"]},
						{"label": "Returns", "items": ["Purchase Receipt"]},
					],
				})
			return FakeFrappeDict({"transactions": []})

	fake_frappe.get_meta = lambda dt: FakeMeta(dt)

	fake_frappe._docs_store = docs_store
	fake_frappe._perms_store = perms_store
	fake_frappe._doctype_read_perms = doctype_read_perms

	sys.modules["frappe"] = fake_frappe
	sys.modules["frappe.desk"] = ModuleType("frappe.desk")
	sys.modules["frappe.desk.notifications"] = ModuleType("frappe.desk.notifications")

	def get_filters_for(doctype):
		if doctype in ("Delivery Note", "Purchase Receipt", "Sales Order", "Purchase Order"):
			return {"docstatus": 0}
		return None

	sys.modules["frappe.desk.notifications"].get_filters_for = get_filters_for

	# Mock native _get_linked_document_counts
	def native_get_linked_document_counts(doctype, name, items=None):
		return {
			"count": {
				"internal_links_found": [],
				"external_links_found": [{"doctype": it, "count": 1, "open_count": 0} for it in (items or [])],
			},
			"timeline_data": {"2026-10-31": 1},
		}

	sys.modules["frappe.desk.notifications"]._get_linked_document_counts = native_get_linked_document_counts
	return fake_frappe


class TestConnections(unittest.TestCase):
	def setUp(self):
		self.frappe = _setup_fake_frappe()
		self.frappe._docs_store.clear()
		self.frappe._perms_store.clear()
		self.frappe._doctype_read_perms.clear()
		self.frappe._doctype_read_perms.update({
			"Purchase Order": True,
			"Sales Order": True,
			"Delivery Note": True,
			"Purchase Receipt": True,
		})
		if "intercompany_sync.connections" in sys.modules:
			del sys.modules["intercompany_sync.connections"]
		if "intercompany_sync" in sys.modules and hasattr(sys.modules["intercompany_sync"], "connections"):
			delattr(sys.modules["intercompany_sync"], "connections")

	def tearDown(self):
		for mod in list(sys.modules.keys()):
			if mod not in _ORIGINAL_MODULES:
				del sys.modules[mod]
		sys.modules.update(_ORIGINAL_MODULES)

	def _create_full_intercompany_chain(self):
		"""Set up PO-1 <-> SO-1 <-> DN-1 <-> PR-1 chain with item rows."""
		po = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-001",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-001",
			"items": [
				FakeDoc({
					"name": "POI-001",
					"item_code": "ITEM-1",
					"qty": 10,
					"sales_order": "SO-001",
					"sales_order_item": "SOI-001",
				}),
				FakeDoc({
					"name": "POI-002",
					"item_code": "ITEM-2",
					"qty": 5,
					"sales_order": "SO-001",
					"sales_order_item": "SOI-002",
				}),
			],
		})

		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-001",
			"items": [
				FakeDoc({
					"name": "SOI-001",
					"item_code": "ITEM-1",
					"qty": 10,
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-001",
				}),
				FakeDoc({
					"name": "SOI-002",
					"item_code": "ITEM-2",
					"qty": 5,
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-002",
				}),
			],
		})

		dn = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_reference": "PR-001",
			"items": [
				FakeDoc({
					"name": "DNI-001",
					"item_code": "ITEM-1",
					"qty": 10,
					"against_sales_order": "SO-001",
					"so_detail": "SOI-001",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-001",
				}),
				FakeDoc({
					"name": "DNI-002",
					"item_code": "ITEM-2",
					"qty": 5,
					"against_sales_order": "SO-001",
					"so_detail": "SOI-002",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-002",
				}),
			],
		})

		pr = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-001",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-001",
			"supplier_delivery_note": "DN-001",
			"items": [
				FakeDoc({
					"name": "PRI-001",
					"item_code": "ITEM-1",
					"qty": 10,
					"delivery_note_item": "DNI-001",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-001",
					"sales_order": "SO-001",
					"sales_order_item": "SOI-001",
				}),
				FakeDoc({
					"name": "PRI-002",
					"item_code": "ITEM-2",
					"qty": 5,
					"delivery_note_item": "DNI-002",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-002",
					"sales_order": "SO-001",
					"sales_order_item": "SOI-002",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-001")] = po
		self.frappe._docs_store[("Sales Order", "SO-001")] = so
		self.frappe._docs_store[("Delivery Note", "DN-001")] = dn
		self.frappe._docs_store[("Purchase Receipt", "PR-001")] = pr

		return po, so, dn, pr

	# --- Dashboard Enrichers ---

	def test_purchase_order_dashboard_enricher_appends_missing_and_sets_method(self):
		"""purchase_order enricher appends missing Delivery Note and sets method."""
		from intercompany_sync import connections

		raw_data = {
			"fieldname": "purchase_order",
			"transactions": [
				{"label": "Related", "items": ["Purchase Receipt", "Purchase Invoice", "Sales Order"]},
			],
		}
		enriched = connections.purchase_order(raw_data)
		self.assertEqual(enriched["method"], "intercompany_sync.connections.get_open_count")
		all_items = [it for g in enriched["transactions"] for it in g["items"]]
		self.assertIn("Delivery Note", all_items)
		self.assertIn("Sales Order", all_items)
		self.assertIn("Purchase Receipt", all_items)
		self.assertEqual(enriched["transactions"][0]["label"], "Related")

	def test_sales_order_dashboard_enricher_appends_missing_and_sets_method(self):
		"""sales_order enricher appends missing Purchase Receipt and sets method."""
		from intercompany_sync import connections

		raw_data = {
			"fieldname": "sales_order",
			"transactions": [
				{"label": "Fulfillment", "items": ["Delivery Note", "Sales Invoice"]},
				{"label": "Purchasing", "items": ["Purchase Order"]},
			],
		}
		enriched = connections.sales_order(raw_data)
		self.assertEqual(enriched["method"], "intercompany_sync.connections.get_open_count")
		all_items = [it for g in enriched["transactions"] for it in g["items"]]
		self.assertIn("Purchase Receipt", all_items)
		self.assertIn("Delivery Note", all_items)
		self.assertIn("Purchase Order", all_items)

	def test_delivery_note_dashboard_enricher_preserves_groups_and_sets_method(self):
		"""delivery_note enricher preserves existing groups since SO, PO, PR already present."""
		from intercompany_sync import connections

		raw_data = {
			"fieldname": "delivery_note",
			"transactions": [
				{"label": "Reference", "items": ["Sales Order"]},
				{"label": "Internal Transfer", "items": ["Purchase Order", "Purchase Receipt"]},
			],
		}
		orig_len = len(raw_data["transactions"])
		enriched = connections.delivery_note(raw_data)
		self.assertEqual(enriched["method"], "intercompany_sync.connections.get_open_count")
		self.assertEqual(len(enriched["transactions"]), orig_len)

	def test_purchase_receipt_dashboard_enricher_appends_missing_and_sets_method(self):
		"""purchase_receipt enricher appends missing Sales Order and Delivery Note."""
		from intercompany_sync import connections

		raw_data = {
			"fieldname": "purchase_receipt_no",
			"transactions": [
				{"label": "Reference", "items": ["Purchase Order"]},
			],
		}
		enriched = connections.purchase_receipt(raw_data)
		self.assertEqual(enriched["method"], "intercompany_sync.connections.get_open_count")
		all_items = [it for g in enriched["transactions"] for it in g["items"]]
		self.assertIn("Sales Order", all_items)
		self.assertIn("Delivery Note", all_items)
		self.assertIn("Purchase Order", all_items)

	# --- Bidirectional Graph Tests ---

	def test_query_from_purchase_order_discovers_so_dn_pr(self):
		"""Querying from PO finds related SO, DN, and PR."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res["Sales Order"], ["SO-001"])
		self.assertEqual(res["Delivery Note"], ["DN-001"])
		self.assertEqual(res["Purchase Receipt"], ["PR-001"])

	def test_query_from_sales_order_discovers_po_dn_pr(self):
		"""Querying from SO finds related PO, DN, and PR."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		res = connections.get_related_documents("Sales Order", "SO-001")
		self.assertEqual(res["Purchase Order"], ["PO-001"])
		self.assertEqual(res["Delivery Note"], ["DN-001"])
		self.assertEqual(res["Purchase Receipt"], ["PR-001"])

	def test_query_from_delivery_note_discovers_so_po_pr(self):
		"""Querying from DN finds related SO, PO, and PR."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		res = connections.get_related_documents("Delivery Note", "DN-001")
		self.assertEqual(res["Sales Order"], ["SO-001"])
		self.assertEqual(res["Purchase Order"], ["PO-001"])
		self.assertEqual(res["Purchase Receipt"], ["PR-001"])

	def test_query_from_purchase_receipt_discovers_dn_so_po(self):
		"""Querying from PR finds related DN, SO, and PO."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Receipt", "PR-001")
		self.assertEqual(res["Delivery Note"], ["DN-001"])
		self.assertEqual(res["Sales Order"], ["SO-001"])
		self.assertEqual(res["Purchase Order"], ["PO-001"])

	# --- Duplicate Items & Multiple Partial Receipts ---

	def test_duplicate_items_produce_distinct_names(self):
		"""Multiple item rows referencing the same parent produce deduplicated names."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		# PO-001 already has two items pointing to SO-001
		res = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res["Sales Order"], ["SO-001"])
		self.assertEqual(len(res["Sales Order"]), 1)

	def test_multiple_partial_receipts_from_single_dn(self):
		"""1 DN -> 2 partial PRs: both PRs are discovered from DN and PO."""
		self._create_full_intercompany_chain()
		# Add a second PR for DN-001
		pr2 = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-002",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-001",
			"supplier_delivery_note": "DN-001",
			"items": [
				FakeDoc({
					"name": "PRI-003",
					"item_code": "ITEM-1",
					"qty": 5,
					"delivery_note_item": "DNI-001",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-001",
				}),
			],
		})
		self.frappe._docs_store[("Purchase Receipt", "PR-002")] = pr2

		from intercompany_sync import connections

		# Check from Delivery Note
		res_dn = connections.get_related_documents("Delivery Note", "DN-001")
		self.assertEqual(res_dn["Purchase Receipt"], ["PR-001", "PR-002"])

		# Check from Purchase Order
		res_po = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res_po["Purchase Receipt"], ["PR-001", "PR-002"])

	# --- Canceled Documents Excluded ---

	def test_canceled_counterparts_are_excluded(self):
		"""Canceled documents (docstatus == 2) are excluded from related results."""
		_po, _so, _dn, pr = self._create_full_intercompany_chain()
		pr.docstatus = 2  # Canceled

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res["Purchase Receipt"], [])
		self.assertEqual(res["Sales Order"], ["SO-001"])

	# --- Hidden Bridge & Permission Safety ---

	def test_hidden_bridge_so_dn_unreadable_pr_readable(self):
		"""When intermediate SO and DN cannot be read by user, PR is still found, SO/DN empty and unrevealed."""
		self._create_full_intercompany_chain()

		# User cannot read Sales Order or Delivery Note
		self.frappe._doctype_read_perms["Sales Order"] = False
		self.frappe._doctype_read_perms["Delivery Note"] = False

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-001")
		# PR is readable and reached
		self.assertEqual(res["Purchase Receipt"], ["PR-001"])
		# SO and DN are empty because user has no read permission
		self.assertEqual(res["Sales Order"], [])
		self.assertEqual(res["Delivery Note"], [])

	def test_no_read_permission_on_target_returns_empty_not_error(self):
		"""Target doctype without read permission returns empty list without error."""
		self._create_full_intercompany_chain()
		self.frappe._doctype_read_perms["Purchase Receipt"] = False

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res["Purchase Receipt"], [])

	# --- No Sibling Overreach ---

	def test_multi_order_so_does_not_overreach_to_unrelated_po_siblings(self):
		"""When SO combines rows from two POs, querying PO-1 only returns DN/PR for PO-1, not PO-2."""
		# PO-1
		po1 = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-001",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-1", "item_code": "ITEM-1", "qty": 10})],
		})
		# PO-2
		po2 = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-002",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-2", "item_code": "ITEM-2", "qty": 20})],
		})

		# Multi-order SO with two rows
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-COMBINED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "SOI-1",
					"item_code": "ITEM-1",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-1",
				}),
				FakeDoc({
					"name": "SOI-2",
					"item_code": "ITEM-2",
					"purchase_order": "PO-002",
					"purchase_order_item": "POI-2",
				}),
			],
		})

		# DN-1 delivering only PO-1's item
		dn1 = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-1",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-COMBINED",
					"so_detail": "SOI-1",
					"purchase_order": "PO-001",
					"purchase_order_item": "POI-1",
				}),
			],
		})

		# DN-2 delivering only PO-2's item
		dn2 = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-002",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-2",
					"item_code": "ITEM-2",
					"against_sales_order": "SO-COMBINED",
					"so_detail": "SOI-2",
					"purchase_order": "PO-002",
					"purchase_order_item": "POI-2",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-001")] = po1
		self.frappe._docs_store[("Purchase Order", "PO-002")] = po2
		self.frappe._docs_store[("Sales Order", "SO-COMBINED")] = so
		self.frappe._docs_store[("Delivery Note", "DN-001")] = dn1
		self.frappe._docs_store[("Delivery Note", "DN-002")] = dn2

		from intercompany_sync import connections

		# PO-001 should only see DN-001, never DN-002
		res1 = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res1["Sales Order"], ["SO-COMBINED"])
		self.assertEqual(res1["Delivery Note"], ["DN-001"])

		# PO-002 should only see DN-002, never DN-001
		res2 = connections.get_related_documents("Purchase Order", "PO-002")
		self.assertEqual(res2["Sales Order"], ["SO-COMBINED"])
		self.assertEqual(res2["Delivery Note"], ["DN-002"])

	# --- Source Permission and Validation Checks ---

	def test_unsupported_doctype_raises_validation_error(self):
		"""Querying unsupported doctype raises ValidationError."""
		from intercompany_sync import connections

		with self.assertRaises(self.frappe.ValidationError):
			connections.get_related_documents("Purchase Invoice", "PI-001")

	def test_source_read_permission_enforced(self):
		"""User without read permission on source document raises PermissionError."""
		self._create_full_intercompany_chain()
		self.frappe._perms_store[("Purchase Order", "read", "PO-001")] = False

		from intercompany_sync import connections

		with self.assertRaises(self.frappe.PermissionError):
			connections.get_related_documents("Purchase Order", "PO-001")

	# --- get_open_count Endpoint ---

	def test_get_open_count_returns_envelope_with_internal_links_and_preserves_unrelated(self):
		"""get_open_count replaces counterpart types with internal_links_found and preserves unrelated."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		# Request items containing counterpart types and an unrelated native type
		res = connections.get_open_count(
			"Purchase Order",
			"PO-001",
			items=["Sales Order", "Delivery Note", "Purchase Receipt", "Payment Entry"],
		)

		self.assertIn("count", res)
		counts = res["count"]
		self.assertIn("internal_links_found", counts)
		self.assertIn("external_links_found", counts)

		# Counterpart doctypes are in internal_links_found with exact names and counts
		internal_map = {d["doctype"]: d for d in counts["internal_links_found"]}
		self.assertIn("Sales Order", internal_map)
		self.assertEqual(internal_map["Sales Order"]["names"], ["SO-001"])
		self.assertEqual(internal_map["Sales Order"]["count"], 1)

		self.assertIn("Delivery Note", internal_map)
		self.assertEqual(internal_map["Delivery Note"]["names"], ["DN-001"])
		self.assertEqual(internal_map["Delivery Note"]["count"], 1)

		self.assertIn("Purchase Receipt", internal_map)
		self.assertEqual(internal_map["Purchase Receipt"]["names"], ["PR-001"])
		self.assertEqual(internal_map["Purchase Receipt"]["count"], 1)

		# Unrelated native Payment Entry preserved in external_links_found
		external_doctypes = [d["doctype"] for d in counts["external_links_found"]]
		self.assertIn("Payment Entry", external_doctypes)

		# Timeline data preserved from native
		self.assertIn("timeline_data", res)

	def test_get_open_count_rejects_arbitrary_caller_supplied_probe(self):
		"""get_open_count rejects doctype not in dashboard transactions with PermissionError."""
		self._create_full_intercompany_chain()
		from intercompany_sync import connections

		with self.assertRaises(self.frappe.PermissionError):
			connections.get_open_count("Purchase Order", "PO-001", items=["User"])

	# --- Regressions for Review Round 1 Findings ---

	def test_mixed_dn_pr_does_not_leak_sibling_lineage_to_po(self):
		"""Finding 1: Mixed DN header must not cause PR-B (delivering only PO-B rows) to appear for PO-A."""
		# PO-A
		po_a = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-A",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-A", "item_code": "ITEM-A", "qty": 10})],
		})
		# PO-B
		po_b = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-B", "item_code": "ITEM-B", "qty": 20})],
		})
		# Mixed DN delivering rows for both PO-A and PO-B
		dn_mixed = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-MIXED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-A",
					"item_code": "ITEM-A",
					"purchase_order": "PO-A",
					"purchase_order_item": "POI-A",
				}),
				FakeDoc({
					"name": "DNI-B",
					"item_code": "ITEM-B",
					"purchase_order": "PO-B",
					"purchase_order_item": "POI-B",
				}),
			],
		})
		# PR-A receives only DNI-A (has header inter_company_reference = DN-MIXED)
		pr_a = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-A",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-MIXED",
			"items": [
				FakeDoc({
					"name": "PRI-A",
					"item_code": "ITEM-A",
					"delivery_note_item": "DNI-A",
					"purchase_order": "PO-A",
					"purchase_order_item": "POI-A",
				}),
			],
		})
		# PR-B receives only DNI-B (also has header inter_company_reference = DN-MIXED)
		pr_b = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-MIXED",
			"items": [
				FakeDoc({
					"name": "PRI-B",
					"item_code": "ITEM-B",
					"delivery_note_item": "DNI-B",
					"purchase_order": "PO-B",
					"purchase_order_item": "POI-B",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-A")] = po_a
		self.frappe._docs_store[("Purchase Order", "PO-B")] = po_b
		self.frappe._docs_store[("Delivery Note", "DN-MIXED")] = dn_mixed
		self.frappe._docs_store[("Purchase Receipt", "PR-A")] = pr_a
		self.frappe._docs_store[("Purchase Receipt", "PR-B")] = pr_b

		from intercompany_sync import connections

		res_a = connections.get_related_documents("Purchase Order", "PO-A")
		self.assertIn("PR-A", res_a["Purchase Receipt"])
		self.assertNotIn("PR-B", res_a["Purchase Receipt"], "PR-B must NOT leak to PO-A via broad DN header lookup")

	def test_backward_dn_and_pr_traversal_resolves_row_only_refs_without_unrelated_so_header_po(self):
		"""Finding 2: Backward DN/PR traversal resolves row-level SO/PO refs and ignores unrelated SO header PO."""
		po_old = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-OLD",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-OLD", "item_code": "ITEM-OLD", "qty": 5})],
		})
		po_target = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-TARGET",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-TARGET", "item_code": "ITEM-TARGET", "qty": 10})],
		})
		# SO has header pointing to PO-OLD, but SOI-TARGET points to PO-TARGET
		so_mixed = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-MIXED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-OLD",
			"items": [
				FakeDoc({
					"name": "SOI-TARGET",
					"item_code": "ITEM-TARGET",
					"purchase_order": "PO-TARGET",
					"purchase_order_item": "POI-TARGET",
				}),
			],
		})
		dn_target = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-TARGET",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-TARGET",
					"item_code": "ITEM-TARGET",
					"against_sales_order": "SO-MIXED",
					"so_detail": "SOI-TARGET",
				}),
			],
		})
		pr_target = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-TARGET",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-TARGET",
			"items": [
				FakeDoc({
					"name": "PRI-TARGET",
					"item_code": "ITEM-TARGET",
					"delivery_note_item": "DNI-TARGET",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-OLD")] = po_old
		self.frappe._docs_store[("Purchase Order", "PO-TARGET")] = po_target
		self.frappe._docs_store[("Sales Order", "SO-MIXED")] = so_mixed
		self.frappe._docs_store[("Delivery Note", "DN-TARGET")] = dn_target
		self.frappe._docs_store[("Purchase Receipt", "PR-TARGET")] = pr_target

		from intercompany_sync import connections

		res_dn = connections.get_related_documents("Delivery Note", "DN-TARGET")
		self.assertEqual(res_dn["Purchase Order"], ["PO-TARGET"])
		self.assertNotIn("PO-OLD", res_dn["Purchase Order"], "Unrelated SO header PO must not leak to DN")

		res_pr = connections.get_related_documents("Purchase Receipt", "PR-TARGET")
		self.assertEqual(res_pr["Purchase Order"], ["PO-TARGET"])
		self.assertNotIn("PO-OLD", res_pr["Purchase Order"], "Unrelated SO header PO must not leak to PR")

	def test_po_item_sales_order_item_discovers_dn_even_without_reverse_so_item_refs(self):
		"""Finding 3: PO Item.sales_order_item direct row ref enables discovering DN without reverse SO refs."""
		po = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-001",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [
				FakeDoc({
					"name": "POI-001",
					"item_code": "ITEM-1",
					"sales_order": "SO-001",
					"sales_order_item": "SOI-DIRECT",
				}),
			],
		})
		# SO-001 item SOI-DIRECT lacks purchase_order / purchase_order_item fields
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "SOI-DIRECT",
					"item_code": "ITEM-1",
				}),
			],
		})
		# DN delivers that SO item
		dn = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-001",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-001",
					"so_detail": "SOI-DIRECT",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-001")] = po
		self.frappe._docs_store[("Sales Order", "SO-001")] = so
		self.frappe._docs_store[("Delivery Note", "DN-001")] = dn

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-001")
		self.assertEqual(res["Delivery Note"], ["DN-001"], "DN must be discovered via PO Item.sales_order_item")

	def test_self_type_native_returns_links_preserved_in_get_open_count(self):
		"""Finding 4: Same-type requested items (e.g. Purchase Receipt on PR dashboard) must delegate to native."""
		pr = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-001",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "PRI-1", "item_code": "ITEM-1"})],
		})
		self.frappe._docs_store[("Purchase Receipt", "PR-001")] = pr

		from intercompany_sync import connections

		# On PR dashboard, transactions includes "Purchase Receipt" under Returns
		res = connections.get_open_count("Purchase Receipt", "PR-001", items=["Purchase Receipt"])
		counts = res["count"]
		# Should be delegated to native and preserved in external_links_found, NOT in cross-company internal_links_found
		ext_types = [d["doctype"] for d in counts["external_links_found"]]
		self.assertIn("Purchase Receipt", ext_types, "Self-type Returns link must delegate to native")
		int_types = [d["doctype"] for d in counts["internal_links_found"]]
		self.assertNotIn("Purchase Receipt", int_types, "Self-type must NOT be treated as cross-company counterpart")

	def test_supplier_delivery_note_free_text_not_used_as_link(self):
		"""Finding 5: supplier_delivery_note free-text name collision alone must not form a link."""
		dn_unrelated = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-COLLIDE",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "DNI-X", "item_code": "ITEM-X"})],
		})
		# PR coincidentally has supplier_delivery_note = 'DN-COLLIDE' as supplier invoice string
		pr_unrelated = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-COLLIDE",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Third-Party Supplier",
			"supplier_delivery_note": "DN-COLLIDE",
			"items": [FakeDoc({"name": "PRI-X", "item_code": "ITEM-X"})],
		})

		self.frappe._docs_store[("Delivery Note", "DN-COLLIDE")] = dn_unrelated
		self.frappe._docs_store[("Purchase Receipt", "PR-COLLIDE")] = pr_unrelated

		from intercompany_sync import connections

		# DN-COLLIDE should NOT link to PR-COLLIDE
		res_dn = connections.get_related_documents("Delivery Note", "DN-COLLIDE")
		self.assertEqual(res_dn["Purchase Receipt"], [])

		# PR-COLLIDE should NOT link to DN-COLLIDE
		res_pr = connections.get_related_documents("Purchase Receipt", "PR-COLLIDE")
		self.assertEqual(res_pr["Delivery Note"], [])

	def test_get_open_count_preserves_open_count_and_derives_open_names(self):
		"""Finding 6: get_open_count derives open_names and open_count using get_filters_for."""
		# SO-001 has two related DNs: DN-DRAFT (docstatus 0) and DN-SUBMITTED (docstatus 1)
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-001",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "SOI-1", "item_code": "ITEM-1"})],
		})
		dn_draft = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-DRAFT",
			"docstatus": 0,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "DNI-D", "item_code": "ITEM-1", "so_detail": "SOI-1"})],
		})
		dn_submitted = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-SUBMITTED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "DNI-S", "item_code": "ITEM-1", "so_detail": "SOI-1"})],
		})

		self.frappe._docs_store[("Sales Order", "SO-001")] = so
		self.frappe._docs_store[("Delivery Note", "DN-DRAFT")] = dn_draft
		self.frappe._docs_store[("Delivery Note", "DN-SUBMITTED")] = dn_submitted

		from intercompany_sync import connections

		res = connections.get_open_count("Sales Order", "SO-001", items=["Delivery Note"])
		internal_map = {d["doctype"]: d for d in res["count"]["internal_links_found"]}
		self.assertIn("Delivery Note", internal_map)
		dn_data = internal_map["Delivery Note"]
		self.assertEqual(dn_data["count"], 2)
		self.assertEqual(dn_data["names"], ["DN-DRAFT", "DN-SUBMITTED"])
		# Open count must be 1 (DN-DRAFT matches docstatus: 0 filter from get_filters_for)
		self.assertEqual(dn_data["open_count"], 1)
		self.assertEqual(dn_data["open_names"], ["DN-DRAFT"])

	# --- Regressions for Review Round 2 Remaining Issues ---

	def test_document_level_pr_header_ref_without_child_lineage_discovers_pr(self):
		"""Round 2 Issue 1: Document-level PR header ref without child lineage is safely discovered."""
		po_doc = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-DOC",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-DOC",
			"items": [FakeDoc({"name": "POI-DOC", "item_code": "ITEM-DOC", "qty": 10})],
		})
		so_doc = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-DOC",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-DOC",
			"items": [
				FakeDoc({
					"name": "SOI-DOC",
					"item_code": "ITEM-DOC",
					"purchase_order": "PO-DOC",
					"purchase_order_item": "POI-DOC",
				}),
			],
		})
		dn_doc = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-DOC",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_reference": "PR-DOC",
			"items": [
				FakeDoc({
					"name": "DNI-DOC",
					"item_code": "ITEM-DOC",
					"against_sales_order": "SO-DOC",
					"so_detail": "SOI-DOC",
					"purchase_order": "PO-DOC",
					"purchase_order_item": "POI-DOC",
				}),
			],
		})
		# PR-DOC has header inter_company_reference = DN-DOC, but items have NO child row refs
		pr_doc = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-DOC",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-DOC",
			"items": [
				FakeDoc({
					"name": "PRI-DOC",
					"item_code": "ITEM-DOC",
					"qty": 10,
					# No delivery_note_item or purchase_order refs on child row
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-DOC")] = po_doc
		self.frappe._docs_store[("Sales Order", "SO-DOC")] = so_doc
		self.frappe._docs_store[("Delivery Note", "DN-DOC")] = dn_doc
		self.frappe._docs_store[("Purchase Receipt", "PR-DOC")] = pr_doc

		from intercompany_sync import connections

		# PO-DOC should discover PR-DOC via per-DN document-level header fallback without conflicting rows
		res_po = connections.get_related_documents("Purchase Order", "PO-DOC")
		self.assertEqual(res_po["Purchase Receipt"], ["PR-DOC"])

		# SO-DOC should discover PR-DOC via per-DN document-level header fallback
		res_so = connections.get_related_documents("Sales Order", "SO-DOC")
		self.assertEqual(res_so["Purchase Receipt"], ["PR-DOC"])

	def test_combined_so_unresolved_so_child_does_not_fallback_to_header_po(self):
		"""Round 2 Issue 2: DN/PR with unresolved SO child in combined SO must NOT output arbitrary header PO."""
		po_a = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-A",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-A", "item_code": "ITEM-A", "qty": 10})],
		})
		# Combined SO has header pointing to PO-A, but SOI-B is from an unknown/unrelated PO
		so_combined = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-COMBINED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-A",
			"items": [
				FakeDoc({
					"name": "SOI-A",
					"item_code": "ITEM-A",
					"purchase_order": "PO-A",
					"purchase_order_item": "POI-A",
				}),
				FakeDoc({
					"name": "SOI-B",
					"item_code": "ITEM-B",
					# No PO ref on row B
				}),
			],
		})
		# DN-B ships only row B
		dn_b = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-B",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-B",
					"item_code": "ITEM-B",
					"against_sales_order": "SO-COMBINED",
					"so_detail": "SOI-B",
				}),
			],
		})
		# PR-B receives only DNI-B
		pr_b = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-B",
			"items": [
				FakeDoc({
					"name": "PRI-B",
					"item_code": "ITEM-B",
					"delivery_note_item": "DNI-B",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-A")] = po_a
		self.frappe._docs_store[("Sales Order", "SO-COMBINED")] = so_combined
		self.frappe._docs_store[("Delivery Note", "DN-B")] = dn_b
		self.frappe._docs_store[("Purchase Receipt", "PR-B")] = pr_b

		from intercompany_sync import connections

		# DN-B should NOT output PO-A
		res_dn = connections.get_related_documents("Delivery Note", "DN-B")
		self.assertEqual(res_dn["Purchase Order"], [], "Unproven header PO-A must NOT be output for DN line B")

		# PR-B should NOT output PO-A
		res_pr = connections.get_related_documents("Purchase Receipt", "PR-B")
		self.assertEqual(res_pr["Purchase Order"], [], "Unproven header PO-A must NOT be output for PR line B")

	def test_pr_direct_po_ref_with_po_header_so_discovers_so(self):
		"""Round 2 Issue 3: PR with direct PO ref restores document-level PO->SO when no conflicting line refs."""
		so_linked = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-LINKED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-LINKED",
			"items": [FakeDoc({"name": "SOI-L", "item_code": "ITEM-L", "qty": 10})],
		})
		po_linked = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-LINKED",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-LINKED",
			"items": [FakeDoc({"name": "POI-L", "item_code": "ITEM-L", "qty": 10})],
		})
		pr_direct = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-DIRECT",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [
				FakeDoc({
					"name": "PRI-L",
					"item_code": "ITEM-L",
					"purchase_order": "PO-LINKED",
					"purchase_order_item": "POI-L",
					# No direct sales_order field set on PR row
				}),
			],
		})

		self.frappe._docs_store[("Sales Order", "SO-LINKED")] = so_linked
		self.frappe._docs_store[("Purchase Order", "PO-LINKED")] = po_linked
		self.frappe._docs_store[("Purchase Receipt", "PR-DIRECT")] = pr_direct

		from intercompany_sync import connections

		res_pr = connections.get_related_documents("Purchase Receipt", "PR-DIRECT")
		self.assertEqual(res_pr["Purchase Order"], ["PO-LINKED"])
		self.assertEqual(res_pr["Sales Order"], ["SO-LINKED"], "PR->PO->SO must restore SO-LINKED via linked PO")

	# --- Regressions for Review Round 3 Issues ---

	def test_mixed_dn_pr_with_only_child_po_item_ref_does_not_leak_to_po(self):
		"""Round 3 Issue 1: PR with only purchase_order_item (scalar purchase_order empty) must not leak to PO-A."""
		po_a = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-A",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-A", "item_code": "ITEM-A", "qty": 10})],
		})
		po_b = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [FakeDoc({"name": "POI-B", "item_code": "ITEM-B", "qty": 20})],
		})
		dn_mixed = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-MIXED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-A",
					"item_code": "ITEM-A",
					"purchase_order": "PO-A",
					"purchase_order_item": "POI-A",
				}),
				FakeDoc({
					"name": "DNI-B",
					"item_code": "ITEM-B",
					"purchase_order": "PO-B",
					"purchase_order_item": "POI-B",
				}),
			],
		})
		# PR-B has header ref to DN-MIXED, but row only sets purchase_order_item = POI-B (purchase_order is None)
		pr_b = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-MIXED",
			"items": [
				FakeDoc({
					"name": "PRI-B",
					"item_code": "ITEM-B",
					"purchase_order_item": "POI-B",
					# Note: scalar purchase_order and delivery_note_item are None
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-A")] = po_a
		self.frappe._docs_store[("Purchase Order", "PO-B")] = po_b
		self.frappe._docs_store[("Delivery Note", "DN-MIXED")] = dn_mixed
		self.frappe._docs_store[("Purchase Receipt", "PR-B")] = pr_b

		from intercompany_sync import connections

		res_a = connections.get_related_documents("Purchase Order", "PO-A")
		self.assertNotIn(
			"PR-B",
			res_a["Purchase Receipt"],
			"PR-B with only purchase_order_item=POI-B must be identified as conflicting and NOT leak to PO-A",
		)

	def test_pr_with_proven_dn_so_lineage_does_not_add_unrelated_po_header_so(self):
		"""Round 3 Issue 2: PR with actual row proving SO-B via DN must NOT add PO header SO-A."""
		so_a = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-A",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "SOI-A", "item_code": "ITEM-A"})],
		})
		so_b = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-B",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "SOI-B", "item_code": "ITEM-B"})],
		})
		po_linked = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-LINKED",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-A",
			"items": [FakeDoc({"name": "POI-L", "item_code": "ITEM-B"})],
		})
		dn_b = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-B",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-B",
					"item_code": "ITEM-B",
					"against_sales_order": "SO-B",
					"so_detail": "SOI-B",
				}),
			],
		})
		# PR row has DNI-B (which proves SO-B), and also has purchase_order = PO-LINKED
		# But PR row does not have scalar sales_order set directly
		pr = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-PROVEN-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-B",
			"items": [
				FakeDoc({
					"name": "PRI-1",
					"item_code": "ITEM-B",
					"delivery_note_item": "DNI-B",
					"purchase_order": "PO-LINKED",
				}),
			],
		})

		self.frappe._docs_store[("Sales Order", "SO-A")] = so_a
		self.frappe._docs_store[("Sales Order", "SO-B")] = so_b
		self.frappe._docs_store[("Purchase Order", "PO-LINKED")] = po_linked
		self.frappe._docs_store[("Delivery Note", "DN-B")] = dn_b
		self.frappe._docs_store[("Purchase Receipt", "PR-PROVEN-B")] = pr

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Receipt", "PR-PROVEN-B")
		self.assertEqual(res["Sales Order"], ["SO-B"])
		self.assertNotIn("SO-A", res["Sales Order"], "Unrelated PO header SO-A must not override proven row SO-B")

	# --- Regressions for Review Round 4 Issues ---

	def test_pr_with_row_sales_order_item_does_not_add_unrelated_po_header_so(self):
		"""Round 4: PR row with sales_order_item=SOI-B and PO-LINKED (header SO-A) resolves only SO-B and rejects SO-A."""
		so_a = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-A",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "SOI-A", "item_code": "ITEM-A"})],
		})
		so_b = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-B",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [FakeDoc({"name": "SOI-B", "item_code": "ITEM-B"})],
		})
		po_linked = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-LINKED",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-A",
			"items": [FakeDoc({"name": "POI-L", "item_code": "ITEM-B"})],
		})
		pr = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-ROW-SOI-B",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"items": [
				FakeDoc({
					"name": "PRI-1",
					"item_code": "ITEM-B",
					"sales_order_item": "SOI-B",
					"purchase_order": "PO-LINKED",
				}),
			],
		})

		self.frappe._docs_store[("Sales Order", "SO-A")] = so_a
		self.frappe._docs_store[("Sales Order", "SO-B")] = so_b
		self.frappe._docs_store[("Purchase Order", "PO-LINKED")] = po_linked
		self.frappe._docs_store[("Purchase Receipt", "PR-ROW-SOI-B")] = pr

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Receipt", "PR-ROW-SOI-B")
		self.assertEqual(res["Sales Order"], ["SO-B"])
		self.assertNotIn(
			"SO-A",
			res["Sales Order"],
			"Unrelated PO header SO-A must not override proven row SO-B via sales_order_item",
		)





	def test_finding_3_po_header_so_safe_rows_discovers_dn_and_pr(self):
		"""Finding 3: PO header ref to SO without row refs resolves safe SO rows and discovers DN and PR."""
		po = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-HDR-ONLY",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-HDR-SAFE",
			"items": [FakeDoc({"name": "POI-HDR", "item_code": "ITEM-1", "qty": 10})],
		})
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-HDR-SAFE",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-HDR-ONLY",
			"items": [
				FakeDoc({
					"name": "SOI-HDR",
					"item_code": "ITEM-1",
					# No purchase_order or purchase_order_item on row
				}),
			],
		})
		dn = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-FROM-SO",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-FROM-SO",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-HDR-SAFE",
					"so_detail": "SOI-HDR",
				}),
			],
		})
		pr = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-FROM-DN",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-FROM-SO",
			"items": [
				FakeDoc({
					"name": "PRI-FROM-DN",
					"item_code": "ITEM-1",
					"delivery_note_item": "DNI-FROM-SO",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-HDR-ONLY")] = po
		self.frappe._docs_store[("Sales Order", "SO-HDR-SAFE")] = so
		self.frappe._docs_store[("Delivery Note", "DN-FROM-SO")] = dn
		self.frappe._docs_store[("Purchase Receipt", "PR-FROM-DN")] = pr

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-HDR-ONLY")
		self.assertEqual(res["Sales Order"], ["SO-HDR-SAFE"])
		self.assertEqual(res["Delivery Note"], ["DN-FROM-SO"])
		self.assertEqual(res["Purchase Receipt"], ["PR-FROM-DN"])

	def test_finding_3_mixed_so_does_not_broaden_unrelated_rows(self):
		"""Finding 3: Mixed SO with contradictory row PO must NOT broaden to unrelated DN/PR."""
		po = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-MAIN",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-MIXED",
			"items": [FakeDoc({"name": "POI-MAIN", "item_code": "ITEM-1", "qty": 10})],
		})
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-MIXED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-MAIN",
			"items": [
				FakeDoc({
					"name": "SOI-1",
					"item_code": "ITEM-1",
					"purchase_order": "PO-OTHER",
					"purchase_order_item": "POI-OTHER",
				}),
			],
		})
		dn = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-MIXED",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-MIXED",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-MIXED",
					"so_detail": "SOI-1",
				}),
			],
		})
		self.frappe._docs_store[("Purchase Order", "PO-MAIN")] = po
		self.frappe._docs_store[("Sales Order", "SO-MIXED")] = so
		self.frappe._docs_store[("Delivery Note", "DN-MIXED")] = dn

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-MAIN")
		self.assertEqual(res["Delivery Note"], [], "Contradictory row lineage must not be broadened")

	def test_finding_3_conflicting_so_reverse_header_rejects_expansion_no_downstream(self):
		"""Finding 3: If SO reverse header is nonempty and != source PO, reject expansion even if PO forward matches."""
		po_a = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-A",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-X",
			"items": [FakeDoc({"name": "POI-A", "item_code": "ITEM-1", "qty": 10})],
		})
		# SO-X has reverse header pointing to a different PO (PO-B), but rows have no PO ref
		so_x = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-X",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-B",
			"items": [
				FakeDoc({
					"name": "SOI-X",
					"item_code": "ITEM-1",
				}),
			],
		})
		dn_x = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-X",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-X",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-X",
					"so_detail": "SOI-X",
				}),
			],
		})
		pr_x = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-X",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-X",
			"items": [
				FakeDoc({
					"name": "PRI-X",
					"item_code": "ITEM-1",
					"delivery_note_item": "DNI-X",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-A")] = po_a
		self.frappe._docs_store[("Sales Order", "SO-X")] = so_x
		self.frappe._docs_store[("Delivery Note", "DN-X")] = dn_x
		self.frappe._docs_store[("Purchase Receipt", "PR-X")] = pr_x

		from intercompany_sync import connections

		res = connections.get_related_documents("Purchase Order", "PO-A")
		self.assertEqual(res["Delivery Note"], [], "Contradictory reverse header on SO must NOT expand downstream DN")
		self.assertEqual(res["Purchase Receipt"], [], "Contradictory reverse header on SO must NOT expand downstream PR")

	def test_finding_4_batch_pr_fallback_unresolved_child_ids_conflict(self):
		"""Finding 4: PR with unresolved POItem, SOItem, or DNItem child refs must conflict, not silently empty."""
		po = FakeDoc({
			"doctype": "Purchase Order",
			"name": "PO-EXP",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_order_reference": "SO-EXP",
			"items": [FakeDoc({"name": "POI-EXP", "item_code": "ITEM-1", "qty": 10})],
		})
		so = FakeDoc({
			"doctype": "Sales Order",
			"name": "SO-EXP",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"inter_company_order_reference": "PO-EXP",
			"items": [
				FakeDoc({
					"name": "SOI-EXP",
					"item_code": "ITEM-1",
					"purchase_order": "PO-EXP",
					"purchase_order_item": "POI-EXP",
				}),
			],
		})
		dn = FakeDoc({
			"doctype": "Delivery Note",
			"name": "DN-EXP",
			"docstatus": 1,
			"company": "Company B",
			"customer": "Cust-A",
			"items": [
				FakeDoc({
					"name": "DNI-EXP",
					"item_code": "ITEM-1",
					"against_sales_order": "SO-EXP",
					"so_detail": "SOI-EXP",
					"purchase_order": "PO-EXP",
					"purchase_order_item": "POI-EXP",
				}),
			],
		})
		pr_unresolved_poi = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-UNRESOLVED-POI",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-EXP",
			"items": [
				FakeDoc({
					"name": "PRI-1",
					"item_code": "ITEM-1",
					"purchase_order_item": "POI-NONEXISTENT",
				}),
			],
		})
		pr_unresolved_soi = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-UNRESOLVED-SOI",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-EXP",
			"items": [
				FakeDoc({
					"name": "PRI-2",
					"item_code": "ITEM-1",
					"sales_order_item": "SOI-NONEXISTENT",
				}),
			],
		})
		pr_unresolved_dni = FakeDoc({
			"doctype": "Purchase Receipt",
			"name": "PR-UNRESOLVED-DNI",
			"docstatus": 1,
			"company": "Company A",
			"supplier": "Supp-B",
			"inter_company_reference": "DN-EXP",
			"items": [
				FakeDoc({
					"name": "PRI-3",
					"item_code": "ITEM-1",
					"delivery_note_item": "DNI-NONEXISTENT",
				}),
			],
		})

		self.frappe._docs_store[("Purchase Order", "PO-EXP")] = po
		self.frappe._docs_store[("Sales Order", "SO-EXP")] = so
		self.frappe._docs_store[("Delivery Note", "DN-EXP")] = dn
		self.frappe._docs_store[("Purchase Receipt", "PR-UNRESOLVED-POI")] = pr_unresolved_poi
		self.frappe._docs_store[("Purchase Receipt", "PR-UNRESOLVED-SOI")] = pr_unresolved_soi
		self.frappe._docs_store[("Purchase Receipt", "PR-UNRESOLVED-DNI")] = pr_unresolved_dni

		from intercompany_sync import connections

		res_po = connections.get_related_documents("Purchase Order", "PO-EXP")
		self.assertNotIn("PR-UNRESOLVED-POI", res_po["Purchase Receipt"])
		self.assertNotIn("PR-UNRESOLVED-DNI", res_po["Purchase Receipt"])

		res_so = connections.get_related_documents("Sales Order", "SO-EXP")
		self.assertNotIn("PR-UNRESOLVED-SOI", res_so["Purchase Receipt"])
		self.assertNotIn("PR-UNRESOLVED-DNI", res_so["Purchase Receipt"])


if __name__ == "__main__":
	unittest.main()
