-- Migration 005: pre-order/dropship sales — Milestone 7.
--
-- See CLAUDE.md's "Milestone 7 decisions, confirmed 2026-09-14" for the
-- full rationale. Summary: pre-order/dropship inverts every prior
-- milestone's assumption that a purchase/acquisition already happened
-- before anything else occurs — the sale happens FIRST, before any
-- purchase record exists for the item. This migration adds the one new
-- table (`preorder_sales`) needed to record that "sold but not yet
-- acquired" state, plus a linking column on `fungible_depletions` so a
-- later fulfillment (a normal Purchase immediately followed by a real
-- `deplete_fungible()` call — see inventory/preorders.py) can trace back
-- to the pre-order sale it fulfilled, mirroring migration 004's own
-- `ebay_transaction_id` linking-column pattern exactly.
--
-- No sale price/revenue field anywhere here — same standing rule as every
-- prior milestone (Purchases, Depletions, eBay import). Alakazam tracks
-- inventory/cost movement only.

-- ---------------------------------------------------------------------
-- preorder_sales
--
-- status: pending -> fulfilled (via inventory.preorders.fulfill_preorder_sales)
--         pending -> cancelled (via inventory.preorders.cancel_preorder_sale)
-- Both transitions are TERMINAL — no function in this project ever moves a
-- row back out of 'fulfilled'/'cancelled' (same "no correction/reversal
-- flow yet" policy already carried through Purchases/Depletions/eBay
-- import). Cancelling a still-PENDING row is not a correction of a posted
-- fact — nothing has touched inventory/cost for a pending row yet, so this
-- doesn't reopen that gap; see CLAUDE.md.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preorder_sales (
    id              SERIAL PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES items(id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    sale_date       DATE NOT NULL,
    -- Free-text traceability field only (e.g. a manually-typed eBay order
    -- note) — same convention as fungible_depletions.reference. Never a
    -- sale-price/revenue field.
    reference       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'fulfilled', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    fulfilled_at    TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,

    -- Keeps the terminal timestamp columns from ever drifting out of sync
    -- with status — same discipline as migration 003's
    -- chk_serial_units_sold_date_matches_status.
    CONSTRAINT chk_preorder_sales_terminal_timestamp_matches_status CHECK (
        (status = 'pending' AND fulfilled_at IS NULL AND cancelled_at IS NULL)
        OR (status = 'fulfilled' AND fulfilled_at IS NOT NULL AND cancelled_at IS NULL)
        OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND fulfilled_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_preorder_sales_item_id ON preorder_sales (item_id);
CREATE INDEX IF NOT EXISTS ix_preorder_sales_status ON preorder_sales (status);

-- ---------------------------------------------------------------------
-- Fungible-only invariant, enforced at the DATABASE level as a real
-- backstop — not just the application-level pre-check in
-- inventory/preorders.py::record_preorder_sale() — same two-layer
-- discipline as every other cross-table invariant in this project
-- (migration 001's reconciliation trigger, migration 003's negative-stock
-- trigger). Unlike migration 003's trigger, this one needs no advisory
-- lock: an item's identity_mode is set once at creation and never mutated
-- anywhere in this codebase, so there is no concurrent-mutation race to
-- guard against here — this is a plain cross-table validation, not a
-- running-total invariant.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_preorder_sale_item_is_fungible() RETURNS trigger AS $$
DECLARE
    v_identity_mode TEXT;
BEGIN
    SELECT identity_mode INTO v_identity_mode FROM items WHERE id = NEW.item_id;

    IF v_identity_mode IS NULL THEN
        RAISE EXCEPTION 'preorder_sales.item_id % does not reference a real item', NEW.item_id
            USING ERRCODE = '23514';
    END IF;
    IF v_identity_mode <> 'fungible' THEN
        RAISE EXCEPTION
            'Item % is not fungible — pre-order sales are only supported for fungible items in this milestone',
            NEW.item_id
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_preorder_sale_item_is_fungible ON preorder_sales;
CREATE TRIGGER trg_check_preorder_sale_item_is_fungible
    AFTER INSERT ON preorder_sales
    FOR EACH ROW EXECUTE FUNCTION check_preorder_sale_item_is_fungible();

-- ---------------------------------------------------------------------
-- Linking column on fungible_depletions — mirrors migration 004's
-- `ebay_transaction_id` addition exactly. Threaded through
-- inventory/depletions.py::deplete_fungible() as one more optional,
-- backward-compatible, last-appended parameter (`preorder_sale_id`,
-- `None` default) — every pre-Milestone-7 call site is entirely
-- unaffected.
--
-- Nullable + a partial unique index (WHERE preorder_sale_id IS NOT NULL):
-- an ordinary, non-pre-order depletion never touches this column at all.
-- The uniqueness constraint is a real, DB-level belt-and-suspenders
-- backstop on top of `preorder_sales.status`'s own pending->fulfilled
-- compare-and-swap gate (inventory/preorders.py) — even if some future
-- code path called inventory.depletions functions directly with a
-- `preorder_sale_id` that already has a depletion, it could never
-- double-post the same pre-order sale into real inventory movement.
-- ---------------------------------------------------------------------
ALTER TABLE fungible_depletions
    ADD COLUMN IF NOT EXISTS preorder_sale_id INTEGER REFERENCES preorder_sales(id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fungible_depletions_preorder_sale_id
    ON fungible_depletions (preorder_sale_id)
    WHERE preorder_sale_id IS NOT NULL;
