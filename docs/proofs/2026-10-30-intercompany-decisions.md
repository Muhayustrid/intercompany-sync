# Intercompany Enhancement — Execution Decisions

These decisions preserve user authorization and local verification boundaries.
They are not deployment approval.

| Decision | Reason | Cost or limitation |
|---|---|---|
| Do not install host dependencies | No new dependencies authorized | Frappe integration tests remain unavailable locally |
| Continue source implementation despite six baseline Frappe import failures | Approved plan explicitly permits reporting unavailable integration checks | No claim of real native/database/browser verification |
| Do not commit or push | User did not authorize either action | Deliverable remains uncommitted and not Git-backed |
| Package uncommitted diffs including new files without staging | Bundled review tooling assumes committed ranges | Review packages are local artifacts rather than commit ranges |
| Preserve worktree and ledger at finish | Without commits these contain the only code/review state | Local disk remains occupied until integration is chosen |
| Require PR UOM and conversion factor to match DN | Native receipt updater compares raw transaction quantities | Cross-UOM receipt needs a separately reviewed backend change |
| Draft checks original DN capacity; submit checks locked current outstanding | Several drafts may be prepared before receiving | A stale draft can fail at submit after another PR consumes stock |
| Guard PR requires read access to the referenced DN before exposing source details | Interactive validation must not reveal hidden cross-company data | Users receiving internal goods need explicit DN read permission; no new automatic permission bypass |
| Close two load-bearing residuals after final fix review rather than park them | Known native permission-log disclosure and contradictory-header lineage are unsafe to hand off as ready | One narrowly scoped extra verification round; no new feature or site writes |

Source-picker implementation also has an explicit 500-candidate scan bound with
a Frappe warning to narrow text search. It is not unlimited historical pagination.

Final review caught and withdrew a false-positive recommendation to query
`Purchase Order.company_address`: that field does not exist in standard PO schema.
The implementation must keep the verified `shipping_address` projection instead.
