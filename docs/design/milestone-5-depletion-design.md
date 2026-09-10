# Sale-Side Depletion — Milestone 5

Scope check against `../../CLAUDE.md`'s "Milestone 5 decision, confirmed 2026-09-10": turns Alakazam from acquisition-only (purchases only ever increase on-hand stock — the Milestone 2/3 boundary) into real in/out inventory movement. **No sale price or revenue is captured anywhere in this milestone** — Alakazam tracks inventory quantity and cost-basis movement only; revenue recognition stays entirely on Project-Noctrowl's side (see CLAUDE.md's Category tagging section, carried over from Noctrowl's own "revenue-side analytics... not this system" framing).

This document mirrors `docs/design/schema-design.md`'s own format/depth, scoped to exactly this milestone's addition.

## The core decision (already made, not re-litigated here)

**Weighted-average cost** for fungible items: at the moment of each depletion event, cost-per-unit is the moving weighted average across all currently on-hand purchases of that SKU — recomputed fresh at each depletion, not FIFO batch order, not a period-end/periodic average. Serialized items need no such decision — each unit already carries its own individually-tracked `acquired_cost`; depleting one just marks that specific unit sold and removes its own real cost.

A useful, non-obvious property of moving weighted-average cost: **depletion does not itself change the average cost per remaining unit.** Removing N units at the current average proportionally reduces both quantity and cost basis by the same ratio, so the ratio (average cost) is unchanged — only a *new purchase* (adding cost and quantity at a possibly different price) moves the average. This means "recompute fresh at each depletion" and "the average only really changes when a new purchase lands" are the same statement, not two different rules; the fresh-recompute rule is just the honest, always-correct way to get there without maintaining a separate running-average column that could drift out of sync with the underlying purchase/depletion rows.

## Schema

### `fungible_depletions` (new table)

One row per depletion event for a fungible item.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `item_id` | FK -> `items.id` | |
| `quantity` | INTEGER CHECK > 0 | units removed from on-hand stock |
| `unit_cost` | NUMERIC(20,8) CHECK >= 0 | the moving weighted-average cost per unit AT THE MOMENT of this depletion — `on-hand cost basis / on-hand quantity`, computed fresh (see `inventory/depletions.py::deplete_fungible`). Higher precision than the whole-rupiah money columns elsewhere, since it's an intermediate computed ratio, not a value ever actually paid/received. |
| `total_cost` | NUMERIC(20,2) CHECK >= 0 | `round_half_up(unit_cost * quantity)` — the real, AUTHORITATIVE cost-of-goods-depleted figure, always whole rupiah (same `round_half_up` convention as `inventory/allocation.py`). Computed once in Python, from the full-precision ratio, before either column is written — never re-derived from the (necessarily truncated) stored `unit_cost` afterward, so `unit_cost`'s stored precision is a display/reference margin, not a correctness dependency. |
| `depletion_date` | DATE NOT NULL | the booking point — no physical-fulfillment distinction modeled here (same spirit as `purchases.purchase_date` booking at time of payment, not receipt) |
| `reference` | TEXT, nullable | free-text traceability only — e.g. a manually-typed eBay order note. No real eBay ingestion exists yet (Milestone 6, deferred); this is groundwork a human can fill in, same spirit as `purchases.invoice_document_ref`. **Never** a sale-price/revenue field. |
| `created_at` | TIMESTAMPTZ | |

### `serial_units` (widened, not a new table)

Migration 001 already anticipated this: `status` was modeled as a real column from the start, "always `'on_hand'` in this build... so a future depletion milestone widens this CHECK constraint rather than needing a new column." This milestone does exactly that:

| Column | Type | Notes |
|--------|------|-------|
| `status` | TEXT CHECK IN (`'on_hand'`, `'sold'`) | widened from the Milestone 2 single-value CHECK |
| `sold_date` | DATE, nullable | NOT NULL iff `status = 'sold'` (CHECK) |
| `sold_reference` | TEXT, nullable | same free-text traceability field as `fungible_depletions.reference`, same rules |

`acquired_cost` is untouched and IS the cost removed on depletion — no computation needed, unlike the fungible case.

## The negative-stock invariant — the real focus of this milestone

CLAUDE.md's brief treats this with the same seriousness as Milestone 2's reconciliation invariant: **on-hand quantity can never go negative**, enforced as "a real, concurrency-safe backstop — not just an app-level pre-check that a race condition could slip past," verified both by a raw-SQL bypass attempt and by genuine concurrent requests.

### Fungible: DB trigger + advisory lock

`check_fungible_depletion_not_negative()` (`migrations/003_add_depletions.sql`) is an **immediate** (not deferred) `AFTER INSERT` trigger on `fungible_depletions`. Deliberately different from migration 001's reconciliation trigger, which IS deferred to commit — that trigger needs to tolerate a purchase header + N line inserts within one transaction before being checked; this one has no such multi-step insert to wait out, and deferring it would only delay lock acquisition, not add any benefit.

The trigger body:
1. `PERFORM pg_advisory_xact_lock(NEW.item_id)` — a transaction-scoped Postgres advisory lock keyed on the item being depleted. This is the actual concurrency backstop: a second, genuinely concurrent transaction depleting the *same* item blocks here until the first transaction commits or rolls back. Its own subsequent `SUM()` reads then see a fully-serialized, consistent view of "how much has already been depleted" — never a stale read that would let two overlapping depletions both succeed when only one should have.
2. Re-sums `purchase_line_items.quantity` (total ever purchased) vs. `fungible_depletions.quantity` (total ever depleted, including the row just inserted) for that item.
3. Raises if depleted > purchased.

**QA-found bug, fixed 2026-09-10 — deadlock under genuine concurrency.** Step 1 originally used `SELECT identity_mode FROM items WHERE id = NEW.item_id FOR UPDATE` (a real row lock) instead of an advisory lock. QA's own barrier-synchronized (genuinely simultaneous, not event-staggered) concurrency test found this reliably deadlocked — 6/8 trials for a real over-commit, and, more seriously, 7/10 trials even when both concurrent requests were individually well within stock. Root cause: every `INSERT INTO fungible_depletions` referencing item X implicitly takes a `FOR KEY SHARE` lock on the referenced `items` row as part of Postgres's own foreign-key enforcement, *before* this trigger ever runs. `FOR KEY SHARE` is compatible with itself, so two concurrent transactions can both hold it on the same row; when each transaction's trigger then tries to upgrade to `FOR UPDATE` (which conflicts with the other's held `FOR KEY SHARE`), each blocks waiting for the other — a genuine lock-upgrade deadlock, resolved by Postgres aborting one side with `DeadlockDetected` (`SQLSTATE 40P01`), a completely different exception family than the `IntegrityError` `inventory/depletions.py` was catching, so it propagated as an uncaught 500 instead of a clean `InsufficientStockError`. Reproduced directly (confirmed via an artificially-widened race window, `pg_sleep()` inserted before the lock acquisition, to validate the theory deterministically) before applying the fix.

**The fix** replaces the row lock with `pg_advisory_xact_lock(item_id)` — a wholly separate lock space from Postgres's FK-driven row locks, so acquiring it never interacts with the `FOR KEY SHARE` lock the FK check already holds; there is no shared resource for two transactions to form a wait cycle over. Each depletion call acquires exactly ONE lock (this advisory lock) for its entire duration — a single-lock-per-transaction protocol cannot deadlock against another instance of itself, by construction. Transaction-scoped (`_xact_`, not `_session_`), so it releases automatically at commit/rollback — the exact same duration the old row lock held for, a like-for-like swap of lock *type*, not of *how long* exclusivity is held. Re-verified with the same genuinely-simultaneous `threading.Barrier` methodology across many trials (both the over-commit case and the both-valid case) with zero deadlocks; see `tests/test_depletions.py`'s `TestDepleteFungibleGenuineConcurrency` for the barrier-based versions (the original event-staggered test is still present too, since it's a real, valid — if weaker — scenario, not removed).

As defense-in-depth (belt-and-suspenders, since the advisory lock is expected to make a deadlock unreachable here going forward), `inventory/depletions.py::deplete_fungible` also explicitly catches `sqlalchemy.exc.OperationalError` with SQLSTATE `40P01` and translates it into a new, clearly-labeled `DepletionConflictError` (a real `AlakazamError` subclass, so it still surfaces as a clean `400` at the API layer, not a raw `500`) — rather than assuming the structural fix alone is sufficient and leaving any future regression to propagate uncaught again.

The raise uses `USING ERRCODE = '23514'` (the standard `check_violation` SQLSTATE) rather than the default generic SQLSTATE a bare `RAISE EXCEPTION` gets. This is not cosmetic: psycopg2 maps SQLSTATE class `23` (integrity constraint violation) to its own `IntegrityError` family, which SQLAlchemy in turn wraps as `sqlalchemy.exc.IntegrityError` — the exact exception type `inventory/purchases.py` already catches for its own DB-level SKU/serial uniqueness backstop. Using the same SQLSTATE keeps `inventory/depletions.py::deplete_fungible`'s `except IntegrityError` catch consistent with that established pattern, rather than needing a broader/different exception class just for this one trigger.

`inventory/depletions.py::deplete_fungible` also does a **fast, app-level pre-check** first (read current on-hand stats via `inventory.queries.get_item_stats`, reject before ever touching the database if the request is too large) — the two-layer pattern already established in Milestone 2 (pre-check for the common sequential case, real DB constraint as backstop for genuine concurrency). The insert itself happens inside a `conn.begin_nested()` savepoint, exactly like `inventory/purchases.py`'s new-item/serial inserts, so catching the trigger's `IntegrityError` (or, defensively, `OperationalError`) never leaves the caller's whole connection hard-aborted.

### Serialized: atomic compare-and-swap UPDATE, no trigger needed

A serialized unit's "stock" is binary (`on_hand` or `sold`) — there's no aggregate quantity to re-sum, so no trigger is needed at all. `inventory/depletions.py::deplete_serial_unit` performs the whole state change as **one** atomic statement:

```sql
UPDATE serial_units su
SET status = 'sold', sold_date = :sold_date, sold_reference = :reference
FROM items i
WHERE su.item_id = i.id
  AND UPPER(su.serial_id) = UPPER(:serial_id)
  AND su.status = 'on_hand'
  AND (:expected_sku IS NULL OR UPPER(i.sku) = UPPER(:expected_sku))
RETURNING su.id, su.serial_id, su.item_id, su.acquired_cost, i.sku
```

Postgres's own row-level MVCC locking on this `UPDATE` is already a genuine, real concurrency backstop: two concurrent UPDATEs targeting the same row serialize on the row itself. The loser blocks until the winner commits, then re-evaluates its own `WHERE status = 'on_hand'` against the now-`'sold'` row and naturally affects zero rows — never both succeed. This also means the "belongs to a different item than claimed" check (`expected_sku`) is enforced as part of the *same* atomic statement, not a separate check-then-update race window — a mismatched claim simply never enters the `WHERE` match, full stop.

If the `UPDATE` affects zero rows, a **read-only, diagnostic-only** follow-up query (`_diagnose_serial_depletion_failure`) distinguishes *why* (nonexistent / already sold / wrong item) for a clear error message. This is explicitly not the real gate — the atomic `UPDATE` already is — so a harmless race in the diagnostic query (another concurrent depletion changing the row between the failed `UPDATE` and this `SELECT`) can't cause an incorrect depletion, only a slightly imprecise error message in an already-rare edge case.

## On-hand computation

`inventory/queries.py::get_item_stats` (the single place both identity modes' on-hand quantity/cost are computed — already used by `list_items`, `get_category_stats`, and the Item Detail API) now nets purchased against depleted:
- Fungible: `SUM(purchase_line_items.quantity/line_total) - SUM(fungible_depletions.quantity/total_cost)`.
- Serialized: `COUNT(*)`/`SUM(acquired_cost)` over `serial_units` **filtered to `status = 'on_hand'`** — a sold unit's cost is simply excluded, not subtracted after the fact (same end result, simpler query, no risk of double-subtracting).

Every existing caller (Inventory table, category rollup cards, Item Detail's header stats) picks this up automatically with **no caller-side changes** — none of them ever computed on-hand figures themselves; they always deferred to `get_item_stats`/`get_category_stats`. This was verified by an explicit audit of every call site during this milestone (see the milestone report), not assumed.

**Deliberately unaffected by depletion**: `get_fungible_purchase_rows`, `get_purchase_detail`, and `export/csv_builder.py`'s `build_export_rows` all continue to read `purchase_line_items`' own stored `allocated_item_cost`/`shipping_share`/`line_total` verbatim. A purchase's own recorded line figures are historical fact — a later, separate depletion event must never retroactively change them. Verified by a dedicated test (see below) that purchases the same item, depletes some of it, then re-reads the original purchase's line detail and confirms every figure is byte-for-byte identical to what was saved at purchase time.

## Business logic — `inventory/depletions.py`

- `deplete_fungible(conn, sku, quantity, depletion_date=None, reference=None) -> FungibleDepletionResult`
- `deplete_serial_unit(conn, serial_id, expected_sku=None, sold_date=None, reference=None) -> SerialDepletionResult`

Both raise a real, specific exception from `inventory/exceptions.py` (never a bare `ValueError`), matching the existing hierarchy's style:
- `InsufficientStockError` — fungible over-depletion (`available` is `None` when raised from the DB-trigger race path, since the pre-check's read is already stale by then).
- `SerialUnitNotFoundError` / `AlreadyDepletedError` / `ItemMismatchError` — the three serialized rejection reasons.
- `ValidationError` — structurally bad input (zero/negative quantity, wrong identity mode, blank serial ID).

## Web app

### Item Detail — "Mark as Sold"

- **Fungible**: a quantity input (client-side `max` = the page's already-live on-hand quantity, purely for UI guidance — see below) plus an optional free-text reference field and a Submit button. Posts to `POST /api/items/{sku}/deplete`. On success, the page reloads its own data (on-hand qty/cost basis and the purchase-history table both reflect the depletion automatically, since they're both fresh reads through the updated `get_item_stats`/unaffected `get_fungible_purchase_rows` respectively).
- **Serialized**: each `on_hand` row in the per-unit table gets its own "Mark as Sold" button (with an optional reference prompt); each `sold` row instead shows a "Sold" badge with its `sold_date`/`sold_reference`. Posts to `POST /api/items/{sku}/serial-units/{serial_id}/deplete`.

**The server is always authoritative for the negative-stock check** — same "never trust the client" architecture already proven in Milestone 3 for the reconciliation check. The client-side `max` attribute and any client-side over-depletion warning are guidance only; the real gate is `deplete_fungible`'s pre-check plus the DB trigger, and `deplete_serial_unit`'s atomic UPDATE. A regression test (`tests/webapp/test_depletions_api.py`) deliberately POSTs an over-large quantity directly to the API (bypassing the UI entirely) and asserts it's rejected with a 400 and nothing is written.

### New screen: Sales / Depletion Log

Read-only, mirrors Purchase History's shape exactly (a flat list, no expand-to-accordion needed here since a depletion event has no line-item structure to drill into — it's already one flat fact). Columns: Date, Item (SKU + name), Quantity, Unit, Cost of Goods Depleted, Reference. Backed by `GET /api/depletions` (optionally `?sku=...`, used by an Item Detail link into this log, same pattern as Item Detail linking into Purchase History by `purchase_ref`).

**No edit/delete/reverse affordance** — the same deliberate, already-documented gap as Purchase History (see `docs/design/ui-ux-design.md`'s own "No edit/delete affordance" note). Not built speculatively for this milestone.

## Explicitly out of scope (unchanged from the brief)

No sale price/revenue capture. No real eBay ingestion (Milestone 6). No pre-order/dropship state (Milestone 7). No consignment state (Milestone 8). No correction/reversal flow for a posted depletion. No change to how the Milestone 4 export or Milestone 2 purchase logic already work, beyond the on-hand-computation update described above.

## Open questions / assumptions

1. **The reference field's exact shape** — a single free-text field, not a structured eBay-order-lookup or a dropdown of anything. No validation beyond "optional text." Matches the brief's own framing ("a free-text reference field... no real eBay ingestion exists yet, this is just a traceability field a human can fill in").
2. **`deplete_fungible`'s `depletion_date` / `deplete_serial_unit`'s `sold_date` both default to "today"** when not explicitly supplied — no UI date picker was specified in the brief for the Mark-as-Sold action, so the API accepts an optional override (for scripts/tests/back-dating a known real sale) but the shipped UI doesn't expose a date field, keeping the interaction to "quantity/unit + optional reference, one click." Revisit if a real workflow needs to record a depletion for a date other than today.
3. **No bulk/multi-unit "mark N serialized units sold at once" action** — each serialized unit is marked individually, one atomic UPDATE per unit, matching the brief's "a per-unit 'Mark as Sold' action for a serialized item's individual rows" framing literally. A bulk action wasn't asked for; not built speculatively.
4. **Item Detail's serialized-unit table now shows ALL units (on_hand and sold), not just on_hand ones** — a deliberate design choice (not explicit in the brief) so the "Mark as Sold" action can be scoped per-row correctly and so Item Detail stays a full traceability view. The header's on-hand qty/cost stat is unaffected (still on_hand-only, via `get_item_stats`).
