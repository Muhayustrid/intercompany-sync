"""Intercompany Connections and Dashboard Overrides.

Pure dashboard enrichers and permission-safe counterpart document discovery
for Purchase Order, Sales Order, Delivery Note, and Purchase Receipt.
"""

import json

import frappe
from frappe import _

SUPPORTED_DOCTYPES = ("Purchase Order", "Sales Order", "Delivery Note", "Purchase Receipt")
TARGET_DOCTYPES_SET = set(SUPPORTED_DOCTYPES)


def _enrich_dashboard(data, doctype: str):
	"""Enrich dashboard metadata with counterpart doctypes and custom count method."""
	if data is None:
		data = frappe._dict() if hasattr(frappe, "_dict") else {}

	transactions = data.get("transactions")
	if transactions is None:
		transactions = []
		data["transactions"] = transactions

	existing_items = {item for group in transactions for item in (group.get("items") or [])}
	counterparts = [dt for dt in SUPPORTED_DOCTYPES if dt != doctype]
	missing = [dt for dt in counterparts if dt not in existing_items]

	if missing:
		transactions.append({"label": _("Inter Company"), "items": missing})

	data["method"] = "intercompany_sync.connections.get_open_count"
	return data


def purchase_order(data=None):
	return _enrich_dashboard(data, "Purchase Order")


def sales_order(data=None):
	return _enrich_dashboard(data, "Sales Order")


def delivery_note(data=None):
	return _enrich_dashboard(data, "Delivery Note")


def purchase_receipt(data=None):
	return _enrich_dashboard(data, "Purchase Receipt")


def _pluck(results, field="name"):
	"""Safely extract field list from get_all/get_list results whether dicts or scalars."""
	if not results:
		return []
	if isinstance(results[0], dict):
		return [d.get(field) for d in results if d.get(field)]
	return list(results)


def _batch_resolve_per_dn_prs(
	candidates_dn: set[str],
	already_found_prs: set[str],
	expected_po: str | None = None,
	expected_so: str | None = None,
	allowed_dni: set[str] | None = None,
) -> set[str]:
	"""Batch query candidate PRs and child items for candidate DNs, evaluating row conflicts in memory without N+1 loops."""
	if not candidates_dn:
		return set()

	candidate_prs = _pluck(
		frappe.get_all(
			"Purchase Receipt",
			filters={"inter_company_reference": ("in", list(candidates_dn)), "docstatus": ("<", 2)},
			pluck="name",
		)
	)
	unresolved_prs = [pr for pr in candidate_prs if pr not in already_found_prs]
	if not unresolved_prs:
		return set()

	all_pr_items = frappe.get_all(
		"Purchase Receipt Item",
		filters={"parent": ("in", unresolved_prs)},
		fields=[
			"parent",
			"purchase_order",
			"purchase_order_item",
			"sales_order",
			"sales_order_item",
			"delivery_note_item",
		],
	)

	poi_names = {it["purchase_order_item"] for it in all_pr_items if it.get("purchase_order_item")}
	poi_to_po = {}
	if poi_names:
		poi_rows = frappe.get_all(
			"Purchase Order Item",
			filters={"name": ("in", list(poi_names)), "docstatus": ("<", 2)},
			fields=["name", "parent"],
		)
		poi_to_po = {r["name"]: r["parent"] for r in poi_rows if r.get("parent")}

	soi_names = {it["sales_order_item"] for it in all_pr_items if it.get("sales_order_item")}
	soi_to_so = {}
	if soi_names:
		soi_rows = frappe.get_all(
			"Sales Order Item",
			filters={"name": ("in", list(soi_names)), "docstatus": ("<", 2)},
			fields=["name", "parent"],
		)
		soi_to_so = {r["name"]: r["parent"] for r in soi_rows if r.get("parent")}

	dni_names = {it["delivery_note_item"] for it in all_pr_items if it.get("delivery_note_item")}
	candidate_dni: set[str] = set()
	if dni_names:
		dn_rows = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": ("in", list(candidates_dn)), "name": ("in", list(dni_names)), "docstatus": ("<", 2)},
			fields=["name"],
		)
		candidate_dni = {r["name"] for r in dn_rows if r.get("name")}

	items_by_pr: dict[str, list] = {}
	for it in all_pr_items:
		items_by_pr.setdefault(it["parent"], []).append(it)

	accepted_prs = set()
	for pr_name in unresolved_prs:
		items = items_by_pr.get(pr_name, [])
		has_conflict = False
		for it in items:
			# 1. PO check: supplied purchase_order_item must resolve; must match expected_po
			if it.get("purchase_order_item"):
				if it["purchase_order_item"] not in poi_to_po:
					has_conflict = True
					break
				row_po = poi_to_po[it["purchase_order_item"]]
				if expected_po and row_po != expected_po:
					has_conflict = True
					break
			if expected_po and it.get("purchase_order") and it["purchase_order"] != expected_po:
				has_conflict = True
				break

			# 2. SO check: supplied sales_order_item must resolve; must match expected_so
			if it.get("sales_order_item"):
				if it["sales_order_item"] not in soi_to_so:
					has_conflict = True
					break
				row_so = soi_to_so[it["sales_order_item"]]
				if expected_so and row_so != expected_so:
					has_conflict = True
					break
			if expected_so and it.get("sales_order") and it["sales_order"] != expected_so:
				has_conflict = True
				break

			# 3. DN Item check: supplied delivery_note_item must resolve on candidate DN and match allowed_dni
			if it.get("delivery_note_item"):
				if it["delivery_note_item"] not in candidate_dni:
					has_conflict = True
					break
				if allowed_dni and it["delivery_note_item"] not in allowed_dni:
					has_conflict = True
					break

		if not has_conflict:
			accepted_prs.add(pr_name)

	return accepted_prs


def _discover_related(doctype: str, name: str) -> dict[str, set[str]]:
	"""Discover candidate counterpart document names using exact row lineage."""
	candidates = {dt: set() for dt in SUPPORTED_DOCTYPES}

	if doctype == "Purchase Order":
		po = frappe.get_doc("Purchase Order", name)
		if getattr(po, "docstatus", 0) == 2:
			return candidates

		po_items = po.get("items") or []
		po_item_names = [it.get("name") for it in po_items if it.get("name")]

		# 1. Sales Order
		so_item_ids = set()
		for it in po_items:
			if it.get("sales_order"):
				candidates["Sales Order"].add(it.sales_order)
			if it.get("sales_order_item"):
				so_item_ids.add(it.sales_order_item)

		if so_item_ids:
			so_parents = frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", list(so_item_ids)), "docstatus": ("<", 2)},
				fields=["parent"],
			)
			for sop in so_parents:
				if sop.get("parent"):
					candidates["Sales Order"].add(sop["parent"])

		so_items = frappe.get_all(
			"Sales Order Item",
			filters={"purchase_order": name, "docstatus": ("<", 2)},
			fields=["parent", "name"],
		)
		for it in so_items:
			if it.get("name"):
				so_item_ids.add(it["name"])
			if it.get("parent"):
				candidates["Sales Order"].add(it["parent"])

		if po_item_names:
			so_items_by_poi = frappe.get_all(
				"Sales Order Item",
				filters={"purchase_order_item": ("in", po_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			for it in so_items_by_poi:
				if it.get("name"):
					so_item_ids.add(it["name"])
				if it.get("parent"):
					candidates["Sales Order"].add(it["parent"])

		if not candidates["Sales Order"] and po.get("inter_company_order_reference"):
			candidates["Sales Order"].add(po.inter_company_order_reference)

		if not candidates["Sales Order"]:
			so_headers = _pluck(
				frappe.get_all(
					"Sales Order",
					filters={"inter_company_order_reference": name, "docstatus": ("<", 2)},
					pluck="name",
				)
			)
			candidates["Sales Order"].update(so_headers)

		# Resolve safe SO rows for valid whole-document header ref when NO contradictory/unresolved row lineage
		for so_name in list(candidates["Sales Order"]):
			so_hdr_po = frappe.db.get_value("Sales Order", so_name, "inter_company_order_reference")
			if so_hdr_po and so_hdr_po != name:
				continue
			is_whole_doc_header_ref = (
				po.get("inter_company_order_reference") == so_name
				or so_hdr_po == name
			)
			if is_whole_doc_header_ref:
				so_rows = frappe.get_all(
					"Sales Order Item",
					filters={"parent": so_name, "docstatus": ("<", 2)},
					fields=["name", "parent", "purchase_order", "purchase_order_item"],
				)
				has_conflict = False
				for r in so_rows:
					if r.get("purchase_order") and r["purchase_order"] != name:
						has_conflict = True
						break
					if r.get("purchase_order_item") and (
						not po_item_names or r["purchase_order_item"] not in po_item_names
					):
						has_conflict = True
						break
				if not has_conflict:
					for r in so_rows:
						if r.get("name"):
							so_item_ids.add(r["name"])

		# 2. Delivery Note
		dn_items_direct = frappe.get_all(
			"Delivery Note Item",
			filters={"purchase_order": name, "docstatus": ("<", 2)},
			fields=["parent", "name"],
		)
		dn_item_ids = {it["name"] for it in dn_items_direct if it.get("name")}
		candidates["Delivery Note"].update(it["parent"] for it in dn_items_direct if it.get("parent"))

		if po_item_names:
			dn_items_by_poi = frappe.get_all(
				"Delivery Note Item",
				filters={"purchase_order_item": ("in", po_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			dn_item_ids.update(it["name"] for it in dn_items_by_poi if it.get("name"))
			candidates["Delivery Note"].update(it["parent"] for it in dn_items_by_poi if it.get("parent"))

		if so_item_ids:
			dn_items_by_soi = frappe.get_all(
				"Delivery Note Item",
				filters={"so_detail": ("in", list(so_item_ids)), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			dn_item_ids.update(it["name"] for it in dn_items_by_soi if it.get("name"))
			candidates["Delivery Note"].update(it["parent"] for it in dn_items_by_soi if it.get("parent"))

		# 3. Purchase Receipt (exact row lineage)
		pr_items_direct = frappe.get_all(
			"Purchase Receipt Item",
			filters={"purchase_order": name, "docstatus": ("<", 2)},
			fields=["parent", "name"],
		)
		candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_direct if it.get("parent"))

		if po_item_names:
			pr_items_by_poi = frappe.get_all(
				"Purchase Receipt Item",
				filters={"purchase_order_item": ("in", po_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_by_poi if it.get("parent"))

		if dn_item_ids:
			pr_items_by_dni = frappe.get_all(
				"Purchase Receipt Item",
				filters={"delivery_note_item": ("in", list(dn_item_ids)), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_by_dni if it.get("parent"))

		# Per-DN document-level PR header fallback evaluated in batch without N+1 loops
		if candidates["Delivery Note"]:
			safe_prs = _batch_resolve_per_dn_prs(
				candidates["Delivery Note"],
				candidates["Purchase Receipt"],
				expected_po=name,
				allowed_dni=dn_item_ids if dn_item_ids else None,
			)
			candidates["Purchase Receipt"].update(safe_prs)

	elif doctype == "Sales Order":
		so = frappe.get_doc("Sales Order", name)
		if getattr(so, "docstatus", 0) == 2:
			return candidates

		so_items = so.get("items") or []
		so_item_names = [it.get("name") for it in so_items if it.get("name")]

		# 1. Purchase Order
		po_item_ids = set()
		for it in so_items:
			if it.get("purchase_order"):
				candidates["Purchase Order"].add(it.purchase_order)
			if it.get("purchase_order_item"):
				po_item_ids.add(it.purchase_order_item)

		if po_item_ids:
			po_parents = frappe.get_all(
				"Purchase Order Item",
				filters={"name": ("in", list(po_item_ids)), "docstatus": ("<", 2)},
				fields=["parent"],
			)
			for pop in po_parents:
				if pop.get("parent"):
					candidates["Purchase Order"].add(pop["parent"])

		if so_item_names:
			po_items_by_soi = frappe.get_all(
				"Purchase Order Item",
				filters={"sales_order_item": ("in", so_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Order"].update(it["parent"] for it in po_items_by_soi if it.get("parent"))

		if not candidates["Purchase Order"] and so.get("inter_company_order_reference"):
			candidates["Purchase Order"].add(so.inter_company_order_reference)

		if not candidates["Purchase Order"]:
			po_headers = _pluck(
				frappe.get_all(
					"Purchase Order",
					filters={"inter_company_order_reference": name, "docstatus": ("<", 2)},
					pluck="name",
				)
			)
			candidates["Purchase Order"].update(po_headers)

		# 2. Delivery Note
		dn_item_ids = set()
		if so_item_names:
			dn_items = frappe.get_all(
				"Delivery Note Item",
				filters={"so_detail": ("in", so_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			dn_item_ids.update(it["name"] for it in dn_items if it.get("name"))
			candidates["Delivery Note"].update(it["parent"] for it in dn_items if it.get("parent"))

		dn_items_by_so = frappe.get_all(
			"Delivery Note Item",
			filters={"against_sales_order": name, "docstatus": ("<", 2)},
			fields=["parent", "name"],
		)
		dn_item_ids.update(it["name"] for it in dn_items_by_so if it.get("name"))
		candidates["Delivery Note"].update(it["parent"] for it in dn_items_by_so if it.get("parent"))

		# 3. Purchase Receipt (exact row lineage)
		if dn_item_ids:
			pr_items_by_dni = frappe.get_all(
				"Purchase Receipt Item",
				filters={"delivery_note_item": ("in", list(dn_item_ids)), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_by_dni if it.get("parent"))

		if so_item_names:
			pr_items_by_soi = frappe.get_all(
				"Purchase Receipt Item",
				filters={"sales_order_item": ("in", so_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_by_soi if it.get("parent"))

		pr_items_by_so = frappe.get_all(
			"Purchase Receipt Item",
			filters={"sales_order": name, "docstatus": ("<", 2)},
			fields=["parent", "name"],
		)
		candidates["Purchase Receipt"].update(it["parent"] for it in pr_items_by_so if it.get("parent"))

		# Per-DN document-level PR header fallback evaluated in batch without N+1 loops
		if candidates["Delivery Note"]:
			safe_prs = _batch_resolve_per_dn_prs(
				candidates["Delivery Note"],
				candidates["Purchase Receipt"],
				expected_so=name,
				allowed_dni=dn_item_ids if dn_item_ids else None,
			)
			candidates["Purchase Receipt"].update(safe_prs)

	elif doctype == "Delivery Note":
		dn = frappe.get_doc("Delivery Note", name)
		if getattr(dn, "docstatus", 0) == 2:
			return candidates

		dn_items = dn.get("items") or []
		dn_item_names = [it.get("name") for it in dn_items if it.get("name")]

		# 1. Sales Order
		so_item_ids = set()
		for it in dn_items:
			if it.get("so_detail"):
				so_item_ids.add(it.so_detail)
			elif it.get("against_sales_order"):
				candidates["Sales Order"].add(it.against_sales_order)

		so_items_resolved = []
		if so_item_ids:
			so_items_resolved = frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", list(so_item_ids)), "docstatus": ("<", 2)},
				fields=["name", "parent", "purchase_order", "purchase_order_item"],
			)
			for sop in so_items_resolved:
				if sop.get("parent"):
					candidates["Sales Order"].add(sop["parent"])

		# 2. Purchase Order
		po_item_ids = set()
		for it in dn_items:
			if it.get("purchase_order"):
				candidates["Purchase Order"].add(it.purchase_order)
			if it.get("purchase_order_item"):
				po_item_ids.add(it.purchase_order_item)

		for sop in so_items_resolved:
			if sop.get("purchase_order"):
				candidates["Purchase Order"].add(sop["purchase_order"])
			if sop.get("purchase_order_item"):
				po_item_ids.add(sop["purchase_order_item"])
			elif not sop.get("purchase_order") and sop.get("parent"):
				# Prove header PO applies to this child row; do not guess on multi-order SO
				so_hdr_po = frappe.db.get_value(
					"Sales Order", sop["parent"], "inter_company_order_reference"
				)
				if so_hdr_po:
					is_proven = frappe.get_all(
						"Purchase Order Item",
						filters={
							"parent": so_hdr_po,
							"sales_order_item": sop["name"],
							"docstatus": ("<", 2),
						},
						fields=["parent"],
					)
					if is_proven:
						candidates["Purchase Order"].add(so_hdr_po)

		if po_item_ids:
			po_parents = frappe.get_all(
				"Purchase Order Item",
				filters={"name": ("in", list(po_item_ids)), "docstatus": ("<", 2)},
				fields=["parent"],
			)
			for pop in po_parents:
				if pop.get("parent"):
					candidates["Purchase Order"].add(pop["parent"])

		# Document-level fallback ONLY if no row-level SO child items were present on DN
		if not candidates["Purchase Order"] and not so_item_ids and candidates["Sales Order"]:
			for so_name in list(candidates["Sales Order"]):
				so_hdr_po = frappe.db.get_value(
					"Sales Order", so_name, "inter_company_order_reference"
				)
				if so_hdr_po:
					candidates["Purchase Order"].add(so_hdr_po)

		# 3. Purchase Receipt (exact row lineage + reverse PR lookup by DN ref)
		if dn_item_names:
			pr_items = frappe.get_all(
				"Purchase Receipt Item",
				filters={"delivery_note_item": ("in", dn_item_names), "docstatus": ("<", 2)},
				fields=["parent", "name"],
			)
			candidates["Purchase Receipt"].update(it["parent"] for it in pr_items if it.get("parent"))

		prs_by_ref = _pluck(
			frappe.get_all(
				"Purchase Receipt",
				filters={"inter_company_reference": name, "docstatus": ("<", 2)},
				pluck="name",
			)
		)
		candidates["Purchase Receipt"].update(prs_by_ref)

		if dn.get("inter_company_reference"):
			candidates["Purchase Receipt"].add(dn.inter_company_reference)

	elif doctype == "Purchase Receipt":
		pr = frappe.get_doc("Purchase Receipt", name)
		if getattr(pr, "docstatus", 0) == 2:
			return candidates

		pr_items = pr.get("items") or []
		dn_item_ids = {it.get("delivery_note_item") for it in pr_items if it.get("delivery_note_item")}

		# 1. Delivery Note
		if dn_item_ids:
			dn_parents = frappe.get_all(
				"Delivery Note Item",
				filters={"name": ("in", list(dn_item_ids)), "docstatus": ("<", 2)},
				fields=["name", "parent", "against_sales_order", "so_detail", "purchase_order", "purchase_order_item"],
			)
		else:
			dn_parents = []

		for dnp in dn_parents:
			if dnp.get("parent"):
				candidates["Delivery Note"].add(dnp["parent"])

		if pr.get("inter_company_reference"):
			candidates["Delivery Note"].add(pr.inter_company_reference)

		dns_by_ref = _pluck(
			frappe.get_all(
				"Delivery Note",
				filters={"inter_company_reference": name, "docstatus": ("<", 2)},
				pluck="name",
			)
		)
		candidates["Delivery Note"].update(dns_by_ref)

		# 2. Sales Order
		proven_sos = set()
		for it in pr_items:
			if it.get("sales_order"):
				proven_sos.add(it.sales_order)

		dni_so_details = {dnp["so_detail"] for dnp in dn_parents if dnp.get("so_detail")}
		pr_so_details = {it.get("sales_order_item") for it in pr_items if it.get("sales_order_item")}
		so_detail_ids = dni_so_details | pr_so_details
		so_items_resolved = []
		if so_detail_ids:
			so_items_resolved = frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", list(so_detail_ids)), "docstatus": ("<", 2)},
				fields=["name", "parent", "purchase_order", "purchase_order_item"],
			)
			for sop in so_items_resolved:
				if sop.get("parent"):
					proven_sos.add(sop["parent"])

		for dnp in dn_parents:
			if dnp.get("against_sales_order"):
				proven_sos.add(dnp["against_sales_order"])

		candidates["Sales Order"].update(proven_sos)

		# 3. Purchase Order
		po_item_ids = set()
		for it in pr_items:
			if it.get("purchase_order"):
				candidates["Purchase Order"].add(it.purchase_order)
			if it.get("purchase_order_item"):
				po_item_ids.add(it.purchase_order_item)

		for dnp in dn_parents:
			if dnp.get("purchase_order"):
				candidates["Purchase Order"].add(dnp["purchase_order"])
			if dnp.get("purchase_order_item"):
				po_item_ids.add(dnp["purchase_order_item"])

		for sop in so_items_resolved:
			if sop.get("purchase_order"):
				candidates["Purchase Order"].add(sop["purchase_order"])
			if sop.get("purchase_order_item"):
				po_item_ids.add(sop["purchase_order_item"])
			elif not sop.get("purchase_order") and sop.get("parent"):
				so_hdr_po = frappe.db.get_value(
					"Sales Order", sop["parent"], "inter_company_order_reference"
				)
				if so_hdr_po:
					is_proven = frappe.get_all(
						"Purchase Order Item",
						filters={
							"parent": so_hdr_po,
							"sales_order_item": sop["name"],
							"docstatus": ("<", 2),
						},
						fields=["parent"],
					)
					if is_proven:
						candidates["Purchase Order"].add(so_hdr_po)

		if po_item_ids:
			po_parents = frappe.get_all(
				"Purchase Order Item",
				filters={"name": ("in", list(po_item_ids)), "docstatus": ("<", 2)},
				fields=["parent"],
			)
			for pop in po_parents:
				if pop.get("parent"):
					candidates["Purchase Order"].add(pop["parent"])

		# Document-level fallback ONLY if no row-level SO child items were present
		if not candidates["Purchase Order"] and not so_detail_ids and candidates["Sales Order"]:
			for so_name in list(candidates["Sales Order"]):
				so_hdr_po = frappe.db.get_value(
					"Sales Order", so_name, "inter_company_order_reference"
				)
				if so_hdr_po:
					candidates["Purchase Order"].add(so_hdr_po)

		# Restore document-level PO -> SO when actual linked PO and no conflicting line refs
		if candidates["Purchase Order"]:
			for po_name in list(candidates["Purchase Order"]):
				po_so_hdr = frappe.db.get_value(
					"Purchase Order", po_name, "inter_company_order_reference"
				)
				if po_so_hdr and (not proven_sos or po_so_hdr in proven_sos):
					candidates["Sales Order"].add(po_so_hdr)

				so_by_hdr = _pluck(
					frappe.get_all(
						"Sales Order",
						filters={"inter_company_order_reference": po_name, "docstatus": ("<", 2)},
						pluck="name",
					)
				)
				for so_cand in so_by_hdr:
					if not proven_sos or so_cand in proven_sos:
						candidates["Sales Order"].add(so_cand)

	return candidates


@frappe.whitelist()
def get_related_documents(doctype: str, name: str) -> dict[str, list[str]]:
	"""Return permission-filtered related documents across PO/SO/DN/PR."""
	if doctype not in TARGET_DOCTYPES_SET:
		frappe.throw(_("Unsupported DocType: {0}").format(doctype), frappe.ValidationError)

	if not frappe.has_permission(doctype, "read", doc=name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca {0} {1}.").format(doctype, name)
		)

	raw_candidates = _discover_related(doctype, name)
	result: dict[str, list[str]] = {}

	for target_dt in SUPPORTED_DOCTYPES:
		if target_dt == doctype:
			continue
		candidates = raw_candidates.get(target_dt, set())
		candidates.discard(name)
		if not candidates or not frappe.has_permission(target_dt, "read"):
			result[target_dt] = []
			continue

		try:
			accessible = _pluck(
				frappe.get_list(
					target_dt,
					filters={"name": ("in", list(candidates)), "docstatus": ("<", 2)},
					pluck="name",
					ignore_permissions=False,
				)
			)
		except (frappe.PermissionError, frappe.ValidationError):
			accessible = []

		filtered = [
			doc_name
			for doc_name in accessible
			if frappe.has_permission(target_dt, "read", doc=doc_name)
		]
		result[target_dt] = sorted(set(filtered))

	return result


@frappe.whitelist()
def get_open_count(doctype: str, name: str, items=None):
	"""Get count for internal and external links, replacing counterpart types with internal links."""
	if doctype not in TARGET_DOCTYPES_SET:
		frappe.throw(_("Unsupported DocType: {0}").format(doctype), frappe.ValidationError)

	if not frappe.has_permission(doctype, "read", doc=name):
		raise frappe.PermissionError(
			_("Anda tidak memiliki izin membaca {0} {1}.").format(doctype, name)
		)

	meta = frappe.get_meta(doctype)
	dashboard_data = meta.get_dashboard_data() if hasattr(meta, "get_dashboard_data") else None

	allowed_items = set()
	if dashboard_data and getattr(dashboard_data, "transactions", None):
		for grp in dashboard_data.transactions:
			allowed_items.update(grp.get("items") or [])

	if items is None:
		requested_items = list(allowed_items)
	else:
		if isinstance(items, str):
			requested_items = json.loads(items)
		else:
			requested_items = list(items)

	# Validate requested items against allowed dashboard items
	for item in requested_items:
		if allowed_items and item not in allowed_items:
			raise frappe.PermissionError(_("Invalid dashboard item probe: {0}").format(item))

	# Counterparts strictly exclude the source doctype itself (e.g. Purchase Receipt on PR dashboard)
	counterpart_items = [d for d in requested_items if d in TARGET_DOCTYPES_SET and d != doctype]
	# Unrelated items include non-counterpart doctypes AND same-type native links (e.g. PR Returns on PR)
	unrelated_items = [d for d in requested_items if d not in TARGET_DOCTYPES_SET or d == doctype]

	internal_links_found = []
	external_links_found = []
	timeline_data = None

	if unrelated_items:
		try:
			from frappe.desk.notifications import _get_linked_document_counts

			native_counts = _get_linked_document_counts(doctype, name, items=unrelated_items)
			if isinstance(native_counts, dict):
				c = native_counts.get("count", {})
				internal_links_found.extend(c.get("internal_links_found", []))
				external_links_found.extend(c.get("external_links_found", []))
				if "timeline_data" in native_counts:
					timeline_data = native_counts["timeline_data"]
		except (ImportError, AttributeError, KeyError, frappe.ValidationError):
			pass

	related_docs = get_related_documents(doctype, name)
	for dt in counterpart_items:
		doc_names = related_docs.get(dt, [])
		open_names = []
		if doc_names:
			open_filters = None
			try:
				from frappe.desk.notifications import get_filters_for

				open_filters = get_filters_for(dt)
			except (ImportError, AttributeError, KeyError):
				open_filters = None

			if open_filters:
				filters = {"name": ("in", doc_names), "docstatus": ("<", 2)}
				filters.update(open_filters)
				try:
					open_names = _pluck(
						frappe.get_list(
							dt,
							filters=filters,
							pluck="name",
							ignore_permissions=False,
						)
					)
					open_names = sorted(set(open_names))
				except (frappe.PermissionError, frappe.ValidationError):
					open_names = []

		internal_links_found.append({
			"doctype": dt,
			"names": doc_names,
			"open_names": open_names,
			"count": len(doc_names),
			"open_count": len(open_names),
		})

	res = {
		"count": {
			"internal_links_found": internal_links_found,
			"external_links_found": external_links_found,
		}
	}
	if timeline_data:
		res["timeline_data"] = timeline_data
	return res
