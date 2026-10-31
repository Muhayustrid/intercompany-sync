# Intercompany Order/Receipt Connections — Verification

## Authorized dev update — 2026-10-31

- User explicitly approved `dev.localhost` installation/update.
- Backup: `backups/dev-20261031-162633/` (database and site files).
- Existing installed `intercompany_sync` updated; no reinstall or schema migration
  required because changes add scripts/hooks/functions, not DocTypes or fields.
- Clean main app checkout received the approved worktree patch; no commit/push.
- Approved source/tests hot-copied to dev backend/scheduler/queues/websocket,
  processes restarted and site cache cleared. No production service touched.
- Container Frappe16.52.0 / ERPNext16.51.1:107 Python contracts and34 Node passed.
- PO/SO/DN/PR scripts, dashboard count method and PR.before_submit hook verified.
- Read-only existing-document Connections and picker smoke checks passed; ping pong.
- Five production source checksums match main app checkout and dev backend.
- No order/receipt save/submit or browser interaction test performed.
- Hot-copy is not image-persistent; rebuild later before recreating dev containers.

The source-only boundaries below describe the initial delivery, superseded only
for this explicitly authorized dev update.

## Scope and authorization

Approved: PO Company B → SO Company A → DN Company A → partial PR Company B,
plus permission-safe Connections. Work is isolated in an explicitly approved
OpenCode worktree, detached at `dde83adc3d1b05f5a0fbbcde7e65c0c67eaff0c6`.
No site installation, migration, production-derived transaction writes, commits or
pushes are authorized. The original app checkout remains unchanged.
The separate `order_update_pricing` app and its existing dev installation are not
part of this worktree enhancement.

## Current results

| Deliverable | Evidence | Review |
|---|---|---|
| Task 1: mapper APIs and exact child references | 45 isolated Python contract tests passed; focused Ruff passed | Accepted after fixes |
| Task 2: receipt validation and locking protocol | 67 combined Python contract tests passed; focused six-file Ruff passed | Accepted after fixes |
| Task 3: form actions | 29 Node passed;70 Python contracts passed on prior revision | Accepted after fixes |
| Task 4: Connections | 101 Python and34 Node contracts passed | Accepted after fixes |
| Whole implementation | Final107 Python and34 Node passed,0 skipped with explicit local schema path | Source review findings closed |

The coordinator independently ran the reported passing commands. Task reports and
review history are retained in the local ignored plan ledger directory.
Execution decisions and their costs are summarized in
`2026-10-30-intercompany-decisions.md` beside this proof.

## Baseline and remaining checks

- Full host unittest discovery after residual fixes: 113 tests discovered, 107 contract tests
  passed, six existing integration modules failed to import the absent `frappe`
  runtime: `test_cancel`, `test_guards`, `test_juri_payment_job`,
  `test_purchase_invoice_job`, `test_settings`, `test_sync_log_report`.
- Fake-module contamination during test discovery was corrected in Task 2.
- Final schema-aware command used `ERPNEXT_PATH` pointing to the local ERPNext
  Python package. Without an available external schema, the portable contract
  suite reports105 passed/2 explicit skips rather than failing a machine path.
- Pre-existing repository Ruff failure: I001 at `tests/test_cancel.py:388`.
- Focused Ruff on all nine changed/new Python files: passed.
- Python AST and production JS syntax: passed after Task4;82 literal query
  projection fields matched local native schema JSON in a static spot check.
- No real MariaDB parallel transaction test, native mapper integration, Stock
  Ledger/GL verification, or browser interaction has run. Stubbed contract tests
  cannot establish those properties. Scratch-site testing needs separate approval.

## Deliberate bounds

- One source per native intercompany header; occupied targets are rejected rather
  than silently erased or combined. Exact native row identities are required.
- Source search scans at most 500 permitted candidates and warns via Frappe messages
  if users need to narrow text search; this is not unrestricted historical pagination.
- PR transaction UOM and conversion factor must match DN because the native receipt
  updater compares raw transaction quantities. Partial Qty remains supported.
- Draft checks use original dispatched capacity; locked submit checks current
  outstanding. Valid drafts may become stale and fail submit after another receipt.

## Deferred runtime acceptance

- Submitted PO→SO→DN→PR preserves duplicate Item Code row identities.
- DN100→PR60→PR40 and cancellation restore the intended received allocation.
- Accepted/Rejected/Received quantities, taxes, currency precision and totals remain coherent.
- Concurrent PR submits and DN cancellation serialize under real database locks.
- Invalid party/company/warehouse/row references fail without leaking hidden documents.
- Unauthorized DN references in a PR fail before source customer/company/item details
  are disclosed by guard validation messages.
- UI failed/delayed RPC leaves user edits intact; repeated script loads do not
  duplicate handlers; DN Create remains usable after a first partial receipt.
- Connections counts and click lists match exactly the permitted document names,
  without unrelated sibling documents from graph overreach.
- Native Returns connections and open-notification filtered lists remain functional;
  supplier free-text references do not create false internal links.
- Existing invoice/payment automation and cancellation integration tests remain green.
