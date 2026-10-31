# Intercompany Sync

Intercompany transaction automation for JURI and outlets on ERPNext v16.

## Intercompany order and receipt workflow

Supports the cross-company manual sequence:
`Purchase Order (buyer) → Sales Order (seller) → Delivery Note (seller) → Purchase Receipt (buyer)`.

- **Draft Sales Order**: **Get Items From → Inter Company Purchase Order**.
- **Draft Purchase Receipt**: **Get Items From → Inter Company Delivery Note**.
- **Submitted open Delivery Note**: **Create → Inter Company Purchase Receipt** remains available across partial receipts; the server rejects creation when no quantity remains.
- Mapping opens a draft only without auto-saving or auto-submitting.
- Occupied target forms are rejected to prevent overwriting existing data.
- Concurrent draft edits reject stale in-flight responses.
- The custom Create button is not offered on return or closed Delivery Notes; return processing uses native workflows.

### Partial receipts and stock safety

- One Delivery Note supports multiple partial Purchase Receipts (e.g., 100 units as 60 + 40). Received quantity is accepted plus rejected items; unreceived goods are not marked rejected.
- Item tracking follows exact source child rows, including repeated item codes.
- PR transaction UOM and conversion factor must match source DN because native status updates compare raw transaction quantities.
- Draft PRs check original DN capacity. PR submission acquires shared DN locks and current database reads to enforce actual remaining quantities across concurrent submissions. Stale drafts fail at submit once stock is consumed.
- PR cancellation uses native status updates; DN cancellation coordinates with receipt submission through a shared DN lock.

### Source selection and access

- Source lookup enforces interactive user read permissions and native intercompany party representation (no background permission bypass).
- Search scans a maximum of 500 candidate documents per request; users are prompted to narrow search terms if the cap is reached.

### Connections and dashboard navigation

Enhances the document dashboard on Purchase Order, Sales Order, Delivery Note, and Purchase Receipt:

- **Dashboard links**: Missing counterpart links are added under "Inter Company"; existing native groups are preserved, with related count and open status badges.
- **List navigation**: Clicking a connected document count opens a filtered List view restricted to exact matching IDs (`IN` filter). Zero-count links safely return without opening unrestricted list views.
- **Open notifications**: Open counts and open click lists evaluate native status filters against matched documents, preserving native open badges.
- **Preserved native links**: Unrelated native links, timeline heatmaps, and same-type return connections (e.g., PR `return_against`) are preserved.

#### Permissions and privacy

- Requires read access on the active source document.
- Saving/submitting an internal PR also requires read access to its referenced DN;
  a guessed reference does not reveal hidden source details through validation.
- **Hidden bridge isolation**: When tracing across companies through intermediate documents the user cannot read (e.g., intermediate SO or DN), relations are traversed strictly server-side. Unreadable intermediate document names and IDs are never exposed in responses or errors.
- **Target access**: If a user lacks read permission on a counterpart DocType, the count returns 0 and an empty list without raising a 403 error.
- **Probe validation**: Requested dashboard count DocTypes are validated against allowed items.

#### Lineage tracking and historical limits

- Graph navigation follows exact child row references (`purchase_order_item`, `so_detail`, `delivery_note_item`) across transactions, preventing cross-order sibling pollution on consolidated documents.
- Canceled documents (`docstatus == 2`) are excluded.
- Free-text fields (e.g., `supplier_delivery_note`) are not internal links and are never used as linkage proof.
- **Historical missing-link limits**: Pre-existing transactions created without explicit intercompany row references cannot be inferred or linked. This extension does not backfill or alter historical records.

### Development and verification status

- **Dev deployment**: Updated on `dev.localhost` with explicit approval on 2026-10-31 after backup. Approved worktree changes were copied back to the main app checkout and hot-copied to dev application containers. No commit or production deployment was performed.
- Existing invoice/payment hooks are retained; there is no historical backfill and no change to the separate `order_update_pricing` app.
- **Review status**: Task-scoped and whole-worktree source review findings are closed. Final isolated checks passed 107 Python and 34 Node tests with external schema location supplied explicitly. Without ERPNext schemas, two characterization tests skip explicitly.
- **Runtime verification limits**: Contract and Node tests stub framework boundaries and do not prove real MariaDB concurrency, browser rendering, native mapper integration, or Stock/GL ledger entries. Scratch-site testing requires separate authorization.
- Deployment validation must include restricted-user permissions, partial receipt/cancel
  behavior, saved quantities/taxes, and concurrent submissions on a scratch database.
- Dev verification:107 Python contracts and34 Node tests passed inside the container;
  form/dashboard hooks and read-only picker/Connections smoke checks passed on
  Frappe16.52.0 / ERPNext16.51.1. Transaction saves and browser UI remain unverified.
- Deployment is hot-copied, not baked into a rebuilt image. Recreating containers
  from the old image loses these changes; rebuild from the updated app checkout
  before a later container recreation. Do not deploy that shared image to production
  without separate authorization.
- Baseline Ruff `I001` in `intercompany_sync/tests/test_cancel.py:388` and six absent-runtime integration module imports are pre-existing environment limits.

Run available isolated checks from the app repository root:

```sh
python3 -m unittest intercompany_sync.tests.test_mapping intercompany_sync.tests.test_partial_receipt intercompany_sync.tests.test_hooks intercompany_sync.tests.test_connections
node --test intercompany_sync/tests/*.test.js
```
