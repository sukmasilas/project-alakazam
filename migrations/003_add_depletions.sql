-- Migration 003: sale-side depletion — Milestone 5.
--
-- Turns Alakazam from acquisition-only (purchases only ever increase
-- on-hand stock — the Milestone 2/3 boundary) into real in/out inventory
-- movement. See CLAUDE.md's "Milestone 5 decision, confirmed 2026-09-10"
-- and docs/design/milestone-5-depletion-design.md for the full design
-- rationale. No sale price / revenue is captured anywhere here — this is
-- inventory quantity and cost-basis movement only.
--
-- Two genuinely different mechanisms, matching the fixed identity-mode
-- split already established in Milestone 2:
--   - fungible items: a new `fungible_depletions` table, one row per
--     depletion event, carrying a computed moving-weighted-average
--     unit_cost and total_cost.
--   - serialized items: `serial_units.status` (already modeled, always
--     'on_hand' until now — see migration 001's own comment anticipating
--     exactly this) widens to allow 'sold', plus a sold_date/sold_reference
--     pair.

-- ---------------------------------------------------------------------
-- fungible_depletions
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fungible_depletions (
    id              SERIAL PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES items(id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    -- The moving weighted-average cost per unit AT THE MOMENT of this
    -- depletion (on-hand cost basis / on-hand quantity, computed fresh —
    -- see inventory/depletions.py). Kept at higher precision than the
    -- whole-rupiah money columns elsewhere in this schema, since it's an
    -- intermediate computed ratio, not itself a value ever paid/received;
    -- what actually leaves the books is total_cost, always whole rupiah.
    -- 8 decimal places (not the 6 used for e.g. fx_rate_to_idr elsewhere
    -- in this schema) specifically to keep round(unit_cost * quantity)
    -- reproducing total_cost exactly at any realistic quantity — inserted
    -- from the SAME full-precision Python Decimal that computed
    -- total_cost in the first place (inventory/depletions.py never
    -- re-reads the DB-truncated value to derive total_cost), so this is a
    -- belt-and-suspenders precision margin, not a load-bearing dependency.
    unit_cost       NUMERIC(20, 8) NOT NULL CHECK (unit_cost >= 0),
    -- quantity * unit_cost, rounded to the nearest whole rupiah (the same
    -- round_half_up convention used everywhere else in this project) —
    -- the real cost-of-goods-depleted figure, and the authoritative one:
    -- computed once in Python from the full-precision ratio BEFORE either
    -- value is written, never re-derived from the (necessarily truncated)
    -- stored unit_cost afterward.
    total_cost      NUMERIC(20, 2) NOT NULL CHECK (total_cost >= 0),
    depletion_date  DATE NOT NULL,
    -- Free-text traceability field only (e.g. a manually-typed eBay order
    -- note) — never a real eBay ingestion reference in this milestone, and
    -- never a sale-price/revenue field. See module docstring.
    reference       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fungible_depletions_item_id ON fungible_depletions (item_id);

-- ---------------------------------------------------------------------
-- The negative-stock invariant for fungible items, enforced at the
-- DATABASE level as a real, concurrency-safe structural backstop — not
-- just an application-level pre-check (see CLAUDE.md's brief, same
-- seriousness as Milestone 2's reconciliation invariant).
--
-- Unlike migration 001's reconciliation trigger (DEFERRABLE INITIALLY
-- DEFERRED, fires once at commit), this one is a PLAIN, IMMEDIATE AFTER
-- INSERT trigger — deliberately not deferred, because the thing that makes
-- this genuinely concurrency-safe is the LOCK taken at the START of the
-- trigger body: a second, truly concurrent transaction depleting the SAME
-- item blocks on that lock until the first transaction actually commits
-- or rolls back, so its own SUM() reads afterward see a fully-serialized,
-- consistent view of "how much has already been depleted" — never a stale
-- read that lets two overlapping depletions both succeed when only one
-- should have. Deferring this trigger to commit time would only delay
-- when that lock is acquired, not remove the need for it, so there's no
-- benefit to deferring here (unlike the reconciliation trigger, which
-- specifically needs to allow a purchase header + N line inserts within
-- one transaction before being checked).
--
-- QA-found bug, fixed here (2026-09-10): the original version of this
-- trigger used `SELECT identity_mode FROM items WHERE id = NEW.item_id
-- FOR UPDATE` for that lock. That pattern is a real, reliably-reproducible
-- DEADLOCK generator under genuine concurrency, root-caused as follows —
-- every `INSERT INTO fungible_depletions` referencing item X implicitly
-- takes a `FOR KEY SHARE` lock on the referenced `items` row X, as part of
-- Postgres's own foreign-key-constraint enforcement, BEFORE this trigger
-- ever runs. `FOR KEY SHARE` is compatible with itself, so two genuinely
-- concurrent transactions A and B, each inserting their own
-- fungible_depletions row for the SAME item, can both successfully
-- acquire that FK-driven `FOR KEY SHARE` lock on the same row. Then, when
-- this trigger's `... FOR UPDATE` runs for each of them, A tries to
-- upgrade to `FOR UPDATE` — which conflicts with B's already-held `FOR
-- KEY SHARE` — and blocks waiting for B; simultaneously B tries the same
-- upgrade and blocks waiting for A. Neither can ever proceed: a genuine
-- lock-upgrade deadlock, which Postgres's deadlock detector resolves by
-- aborting one side with `DeadlockDetected` (SQLSTATE 40P01) — a
-- completely different exception family than the `IntegrityError`
-- inventory/depletions.py was catching, so it propagated all the way up
-- as an uncaught 500 instead of the clean InsufficientStockError the
-- design promises. Confirmed independently reproducible even when BOTH
-- concurrent requests are individually well within stock (e.g. 3+3 of 10
-- on hand) — this was never actually about the over-commit case, purely
-- about lock-acquisition ordering.
--
-- Fix: lock via `pg_advisory_xact_lock(item_id)` instead of a row lock.
-- An advisory lock is a wholly separate lock space from Postgres's own
-- FK-driven row locks — acquiring it never interacts with, waits on, or
-- conflicts with the `FOR KEY SHARE` lock the FK check already holds, so
-- there is no shared resource for two transactions to form a wait cycle
-- over. Each depletion call only ever acquires ONE lock (this advisory
-- lock, keyed to the one item being depleted) for its entire duration —
-- a single-lock-per-transaction protocol cannot deadlock against another
-- instance of itself, by construction (a cycle needs each participant to
-- be simultaneously holding one resource and waiting on another; with
-- only one lock ever taken here, there's nothing to hold while waiting).
-- Transaction-scoped (`_xact_`, not `_session_`) so it releases
-- automatically at commit/rollback, matching the exact duration the old
-- row lock held for — a like-for-like swap of lock TYPE, not of when/how
-- long exclusivity is held. Verified via a genuinely-simultaneous
-- (`threading.Barrier`-synchronized, not event-staggered) concurrency
-- test — see tests/test_depletions.py — covering both the over-commit
-- case AND the both-requests-individually-valid case, run across many
-- trials.
--
-- Raises with ERRCODE = '23514' (check_violation) rather than a bare
-- default-SQLSTATE RAISE EXCEPTION, specifically so this is caught on the
-- Python side as a real sqlalchemy.exc.IntegrityError — the same
-- exception family inventory/purchases.py already catches for the SKU/
-- serial unique-index backstop — keeping the two-layer
-- pre-check-then-real-constraint pattern consistent across this project.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_fungible_depletion_not_negative() RETURNS trigger AS $$
DECLARE
    v_identity_mode TEXT;
    v_purchased     INTEGER;
    v_depleted      INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(NEW.item_id::bigint);

    SELECT identity_mode INTO v_identity_mode FROM items WHERE id = NEW.item_id;

    IF v_identity_mode IS NULL THEN
        RAISE EXCEPTION 'fungible_depletions.item_id % does not reference a real item', NEW.item_id
            USING ERRCODE = '23514';
    END IF;
    IF v_identity_mode <> 'fungible' THEN
        RAISE EXCEPTION 'Item % is not fungible — cannot post a fungible_depletions row against it', NEW.item_id
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(SUM(quantity), 0) INTO v_purchased
      FROM purchase_line_items WHERE item_id = NEW.item_id;
    SELECT COALESCE(SUM(quantity), 0) INTO v_depleted
      FROM fungible_depletions WHERE item_id = NEW.item_id;

    IF v_depleted > v_purchased THEN
        RAISE EXCEPTION
            'Item % on-hand quantity would go negative: % purchased but % depleted in total',
            NEW.item_id, v_purchased, v_depleted
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_fungible_depletion_not_negative ON fungible_depletions;
CREATE TRIGGER trg_check_fungible_depletion_not_negative
    AFTER INSERT ON fungible_depletions
    FOR EACH ROW EXECUTE FUNCTION check_fungible_depletion_not_negative();

-- ---------------------------------------------------------------------
-- serial_units: widen status to allow 'sold', add sold_date/sold_reference.
--
-- The negative-stock invariant for a serialized unit needs no new trigger
-- at all: a single unit is a binary on_hand/sold state, and
-- inventory/depletions.py::deplete_serial_unit() performs the state change
-- as ONE atomic `UPDATE serial_units SET status = 'sold', ... WHERE
-- status = 'on_hand' AND ...`. Postgres's own MVCC row-level locking on
-- that UPDATE is already a real, genuine concurrency backstop: two
-- concurrent UPDATEs targeting the same row serialize on the row itself —
-- the second one blocks until the first commits, then re-evaluates its
-- WHERE clause against the now-'sold' row and naturally affects zero rows
-- (never both succeed). No custom trigger needed to get that property.
-- ---------------------------------------------------------------------
ALTER TABLE serial_units
    ADD COLUMN IF NOT EXISTS sold_date DATE,
    ADD COLUMN IF NOT EXISTS sold_reference TEXT;

-- Replaces migration 001's original `serial_units_status_check`
-- (`CHECK (status IN ('on_hand'))`) — confirmed live against the real
-- schema that Postgres's default constraint-naming convention names it
-- exactly this (`{table}_{column}_check`), so DROP/ADD by that name is
-- safe and not a guess.
ALTER TABLE serial_units DROP CONSTRAINT IF EXISTS serial_units_status_check;
ALTER TABLE serial_units ADD CONSTRAINT serial_units_status_check
    CHECK (status IN ('on_hand', 'sold'));

-- Keeps sold_date from ever drifting out of sync with status — a sold unit
-- always has a sold_date, an on_hand unit never does.
ALTER TABLE serial_units DROP CONSTRAINT IF EXISTS chk_serial_units_sold_date_matches_status;
ALTER TABLE serial_units ADD CONSTRAINT chk_serial_units_sold_date_matches_status CHECK (
    (status = 'sold' AND sold_date IS NOT NULL)
    OR (status = 'on_hand' AND sold_date IS NULL)
);
