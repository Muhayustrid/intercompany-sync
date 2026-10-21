"""Test data factory for intercompany_sync.

All objects are ICTC-prefixed; every setup function is idempotent.
"""

import frappe
from frappe.utils import get_year_ending, get_year_start, getdate, today

SUPPLIER_CO = "ICTC Supplier"
ICS_ABBR = "ICS"
OUTLET_CO = "ICTC Outlet"
ICO_ABBR = "ICO"
ITEM = "ICTC-ITEM"
PRICE_LIST = "ICTC IC"
INT_CUSTOMER = "ICTC Internal Customer"
INT_SUPPLIER = "ICTC Internal Supplier"
OUTLET_USER = "ictc-outlet@example.com"


def _setup_company(company_name: str, abbr: str) -> None:
	if not frappe.db.exists("Company", company_name):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": company_name,
				"abbr": abbr,
				"default_currency": "IDR",
				"country": "Indonesia",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert()

	cc = frappe.db.get_value("Cost Center", {"company": company_name, "is_group": 0})
	if not cc:
		root_cc = frappe.db.get_value(
			"Cost Center", {"company": company_name, "parent_cost_center": ("is", "not set")}
		)
		cc = (
			frappe.get_doc(
				{
					"doctype": "Cost Center",
					"cost_center_name": "Main",
					"company": company_name,
					"parent_cost_center": root_cc,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)

	bank_group = frappe.db.get_value(
		"Account", {"company": company_name, "account_name": "Bank Accounts", "is_group": 1}
	)
	bank_acct = f"Bank - {abbr}"
	if not frappe.db.exists("Account", bank_acct):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Bank",
				"account_type": "Bank",
				"parent_account": bank_group,
				"company": company_name,
				"account_currency": "IDR",
			}
		).insert()

	wh_name = f"Stores - {abbr}"
	if not frappe.db.exists("Warehouse", wh_name):
		root_wh = frappe.db.get_value("Warehouse", {"company": company_name, "is_group": 1})
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Stores",
				"company": company_name,
				"parent_warehouse": root_wh,
			}
		).insert()

	stock_group = frappe.db.get_value(
		"Account", {"company": company_name, "account_name": "Stock Assets", "is_group": 1}
	)
	stock_acct = f"Stock In Hand - {abbr}"
	if not frappe.db.exists("Account", stock_acct):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Stock In Hand",
				"account_type": "Stock",
				"parent_account": stock_group,
				"company": company_name,
				"account_currency": "IDR",
			}
		).insert()

	frappe.db.set_value("Warehouse", wh_name, "account", stock_acct)

	frappe.db.set_value(
		"Company",
		company_name,
		{
			"cost_center": cc,
			"default_bank_account": bank_acct,
			"enable_perpetual_inventory": 1,
			"default_inventory_account": stock_acct,
		},
	)


def setup_world() -> None:
	"""Idempotent test world setup for inter-company sync."""
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
	if frappe.db.exists("DocType", "IC Sync Settings"):
		frappe.db.set_single_value("IC Sync Settings", "disable_purchase_invoice_sync", 0)
		frappe.db.set_single_value("IC Sync Settings", "disable_juri_payment_sync", 0)

	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc(
			{
				"doctype": "Warehouse Type",
				"__newname": "Transit",
				"warehouse_name": "Transit",
			}
		).insert()

	for name in (
		"Material Receipt",
		"Material Transfer",
		"Material Issue",
	):
		if not frappe.db.exists("Stock Entry Type", name):
			frappe.get_doc(
				{
					"doctype": "Stock Entry Type",
					"__newname": name,
					"purpose": name,
					"is_standard": 1,
				}
			).insert()

	_setup_company(SUPPLIER_CO, ICS_ABBR)
	_setup_company(OUTLET_CO, ICO_ABBR)

	# Fiscal Year covering today
	year = str(getdate(today()).year)
	if not frappe.db.exists("Fiscal Year", year):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": year,
				"year_start_date": get_year_start(today()),
				"year_end_date": get_year_ending(today()),
				"companies": [{"company": SUPPLIER_CO}, {"company": OUTLET_CO}],
			}
		).insert()
	else:
		fy = frappe.get_doc("Fiscal Year", year)
		existing_cos = [d.company for d in fy.companies]
		changed = False
		for co in (SUPPLIER_CO, OUTLET_CO):
			if co not in existing_cos:
				fy.append("companies", {"company": co})
				changed = True
		if changed:
			fy.save()

	# Item Group & UOM
	if not frappe.db.exists("Item Group", "All Item Groups"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "All Item Groups",
				"is_group": 1,
			}
		).insert()
	if not frappe.db.exists("UOM", "Nos"):
		frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": "Nos",
				"must_be_whole_number": 0,
			}
		).insert()

	# Item with defaults
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": ITEM,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"item_defaults": [
					{"company": SUPPLIER_CO, "default_warehouse": f"Stores - {ICS_ABBR}"},
					{"company": OUTLET_CO, "default_warehouse": f"Stores - {ICO_ABBR}"},
				],
			}
		).insert()
	else:
		doc = frappe.get_doc("Item", ITEM)
		existing = {d.company: d for d in doc.item_defaults}
		changed = False
		for co, abbr in ((SUPPLIER_CO, ICS_ABBR), (OUTLET_CO, ICO_ABBR)):
			wh = f"Stores - {abbr}"
			if co not in existing:
				doc.append("item_defaults", {"company": co, "default_warehouse": wh})
				changed = True
			elif existing[co].default_warehouse != wh:
				existing[co].default_warehouse = wh
				changed = True
		if changed:
			doc.save()

	# Price List
	if not frappe.db.exists("Price List", PRICE_LIST):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": PRICE_LIST,
				"currency": "IDR",
				"buying": 1,
				"selling": 1,
			}
		).insert()

	if not frappe.db.exists("Item Price", {"item_code": ITEM, "price_list": PRICE_LIST}):
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": ITEM,
				"price_list": PRICE_LIST,
				"price_list_rate": 1000.0,
			}
		).insert()

	# Customer Group, Supplier Group, Territory
	if not frappe.db.exists("Customer Group", "All Customer Groups"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "All Customer Groups",
				"is_group": 1,
			}
		).insert()

	if not frappe.db.exists("Customer Group", "Commercial"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "Commercial",
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		).insert()

	if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
		frappe.get_doc(
			{
				"doctype": "Supplier Group",
				"supplier_group_name": "All Supplier Groups",
				"is_group": 1,
			}
		).insert()

	if not frappe.db.exists("Territory", "All Territories"):
		frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "All Territories",
				"is_group": 1,
			}
		).insert()

	# Customer & Supplier
	if not frappe.db.exists("Customer", INT_CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": INT_CUSTOMER,
				"customer_group": "Commercial",
				"territory": "All Territories",
				"is_internal_customer": 1,
				"represents_company": OUTLET_CO,
				"companies": [{"company": SUPPLIER_CO}],
			}
		).insert()
	else:
		frappe.db.set_value(
			"Customer",
			INT_CUSTOMER,
			{"is_internal_customer": 1, "represents_company": OUTLET_CO},
		)

	if not frappe.db.exists("Supplier", INT_SUPPLIER):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": INT_SUPPLIER,
				"supplier_group": "All Supplier Groups",
				"is_internal_supplier": 1,
				"represents_company": SUPPLIER_CO,
				"companies": [{"company": OUTLET_CO}],
			}
		).insert()
	else:
		frappe.db.set_value(
			"Supplier",
			INT_SUPPLIER,
			{"is_internal_supplier": 1, "represents_company": SUPPLIER_CO},
		)

	# User
	if not frappe.db.exists("User", OUTLET_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": OUTLET_USER,
				"first_name": "ICTC Outlet",
				"roles": [
					{"role": "Purchase User"},
					{"role": "Accounts User"},
					{"role": "Stock User"},
				],
			}
		).insert()
	else:
		u = frappe.get_doc("User", OUTLET_USER)
		roles = {r.role for r in u.roles}
		changed = False
		for r in ("Purchase User", "Accounts User", "Stock User"):
			if r not in roles:
				u.append("roles", {"role": r})
				changed = True
		if changed:
			u.save()

	if not frappe.db.exists(
		"User Permission", {"user": OUTLET_USER, "allow": "Company", "for_value": OUTLET_CO}
	):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": OUTLET_USER,
				"allow": "Company",
				"for_value": OUTLET_CO,
				"is_default": 1,
			}
		).insert()

	# Stock in Stores - ICS
	from erpnext.stock.utils import get_stock_balance

	if get_stock_balance(ITEM, f"Stores - {ICS_ABBR}") < 100:
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"purpose": "Material Receipt",
				"company": SUPPLIER_CO,
				"items": [
					{
						"item_code": ITEM,
						"t_warehouse": f"Stores - {ICS_ABBR}",
						"qty": 100,
						"basic_rate": 1000,
					}
				],
			}
		)
		se.insert()
		se.submit()


def make_dn(qty: float = 2, rate: float = 1000):
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": SUPPLIER_CO,
			"customer": INT_CUSTOMER,
			"selling_price_list": PRICE_LIST,
			"currency": "IDR",
			"items": [
				{
					"item_code": ITEM,
					"qty": qty,
					"rate": rate,
					"warehouse": f"Stores - {ICS_ABBR}",
				}
			],
		}
	)
	dn.insert()
	dn.submit()
	return dn


def make_pr(dn_name: str):
	from erpnext.stock.doctype.delivery_note.delivery_note import make_inter_company_purchase_receipt

	pr = make_inter_company_purchase_receipt(dn_name)
	for item in pr.items:
		if not item.warehouse:
			item.warehouse = f"Stores - {ICO_ABBR}"
	pr.insert()
	pr.submit()
	return pr


def make_si(dn_name: str, submit: bool = True):
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

	si = make_sales_invoice(dn_name)
	si.insert()
	if submit:
		si.submit()
	return si


def get_pi(si_name: str):
	pi_name = frappe.db.get_value(
		"Purchase Invoice",
		{"inter_company_invoice_reference": si_name, "docstatus": ("<", 2)},
		"name",
	)
	if pi_name:
		return frappe.get_doc("Purchase Invoice", pi_name)
	return None


def pay_pis(allocations: dict[str, float], user: str = "Administrator"):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	first_pi = next(iter(allocations.keys()))
	pe = get_payment_entry("Purchase Invoice", first_pi)
	pe.references = []
	total = 0.0
	for pi_name, alloc_amt in allocations.items():
		pi_doc = frappe.get_doc("Purchase Invoice", pi_name)
		pe.append(
			"references",
			{
				"reference_doctype": "Purchase Invoice",
				"reference_name": pi_name,
				"total_amount": pi_doc.grand_total,
				"outstanding_amount": pi_doc.outstanding_amount,
				"allocated_amount": alloc_amt,
			},
		)
		total += float(alloc_amt)

	pe.paid_amount = total
	pe.received_amount = total
	pe.reference_no = "ICTC-REF"
	pe.reference_date = today()

	orig_user = frappe.session.user
	try:
		frappe.set_user(user)
		pe.insert(ignore_permissions=True)
		pe.submit()
	finally:
		frappe.set_user(orig_user)

	return pe
