-- Migration 004: eBay sales CSV import / review queue — Milestone 6.
--
-- See CLAUDE.md's "Milestone 6 scope decisions, confirmed 2026-09-11" for
-- the full rationale. Summary: there is no reliable machine-matchable
-- identifier linking an eBay export row to an Alakazam item today (Custom
-- label is empty on every real Order row for one account, and populated
-- with an unrelated sourcing reference on the other) — so this is a real
-- review-queue import, not an auto-matcher. Every row requires explicit
-- human confirmation before any depletion posts.
--
-- ---------------------------------------------------------------------
-- ebay_import_batches: one row per uploaded CSV file, plus rollup counts
-- so nothing about the upload is silently discarded from view — even row
-- types this milestone has no inventory use for (Hold / Other fee /
-- Payout / the multi-item "order summary" rows with no item detail) are
-- at least counted and visible, even though they're not stored
-- individually (see ebay_sales_rows below).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ebay_import_batches (
    id                              SERIAL PRIMARY KEY,
    source_filename                 TEXT NOT NULL,
    -- Read from the CSV's own "Seller,<username>" preamble line — not
    -- necessarily the same string as any Alakazam-side account concept;
    -- purely for display/traceability.
    seller                          TEXT,
    uploaded_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_rows_seen                 INTEGER NOT NULL DEFAULT 0,
    order_rows_stored               INTEGER NOT NULL DEFAULT 0,
    -- A re-upload of the same file, or an overlapping month, re-presenting
    -- a Transaction ID already stored from a prior batch. Never
    -- re-inserted as a second reviewable row — see the partial unique
    -- index on ebay_sales_rows below.
    order_rows_duplicate            INTEGER NOT NULL DEFAULT 0,
    -- A real, confirmed CSV quirk (see CLAUDE.md / real sample data): a
    -- multi-item order produces one "Order" row with the net amount but
    -- no Item ID/Transaction ID/Item title (a rollup row), PLUS one
    -- "Order" row per actual line item. The rollup row carries no
    -- matchable item data and is not stored.
    order_summary_rows_skipped      INTEGER NOT NULL DEFAULT 0,
    refund_rows_stored              INTEGER NOT NULL DEFAULT 0,
    -- Hold / Other fee / Payout / anything else — doesn't represent
    -- physical stock movement (see module CLAUDE.md note), not stored,
    -- but counted here so the import summary never silently implies these
    -- rows didn't exist.
    non_actionable_rows_skipped     INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------
-- ebay_sales_rows: one row per actionable CSV line — a real "Order" line
-- item (has its own Item title / Transaction ID), or a "Refund" row
-- (visible for traceability, never actionable — see CLAUDE.md).
--
-- review_status: pending -> matched -> posted, or pending -> skipped.
-- 'posted' is terminal — see CLAUDE.md's "Corrections to a posted row are
-- out of scope for the prototype" (deferred 2026-08-31 on the Noctrowl
-- side, same policy carried over here).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ebay_sales_rows (
    id                      SERIAL PRIMARY KEY,
    batch_id                INTEGER NOT NULL REFERENCES ebay_import_batches(id) ON DELETE CASCADE,
    row_type                TEXT NOT NULL CHECK (row_type IN ('Order', 'Refund')),

    transaction_date        DATE,
    -- The raw "Jul 30, 2026"-style string is kept verbatim alongside the
    -- parsed DATE — if a future export ever uses a date format
    -- strptime('%b %d, %Y') can't parse, transaction_date is simply NULL
    -- rather than the whole row being rejected; the raw text is still
    -- there for a human to read.
    transaction_date_raw    TEXT,
    order_number            TEXT,
    item_id                 TEXT,
    -- The field this migration's uniqueness invariant is keyed on for
    -- "Order" rows — see the partial unique index below and CLAUDE.md's
    -- own verification note (Order number repeats across a multi-item
    -- order; Transaction ID does not, confirmed against real sample data
    -- including a real cross-month duplicate: a Hold-placed/Hold-released
    -- pair in a LATER month's file legitimately reuses the SAME
    -- Transaction ID as an actual Order row from an EARLIER month — which
    -- is exactly why this index is scoped to row_type = 'Order' only, not
    -- a bare global unique constraint on the column).
    ebay_transaction_id     TEXT,
    item_title               TEXT NOT NULL,
    custom_label             TEXT,
    -- NULL for Refund rows (the real export's own Quantity column is "--"
    -- there) — never used for depletion regardless, see review_status.
    quantity                 INTEGER,

    review_status            TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (review_status IN ('pending', 'matched', 'skipped', 'posted')),
    matched_item_id          INTEGER REFERENCES items(id),
    -- Only populated when matched_item_id refers to a serialized item —
    -- the human's specific on-hand-unit picks, one per unit of quantity
    -- (the CSV only ever tells you WHICH ITEM, never WHICH UNIT — see
    -- CLAUDE.md). A JSON array of serial_id strings, length == quantity.
    matched_serial_ids       JSONB,
    -- Set when process_confirmed_rows() fails to post this row (e.g. the
    -- matched item went out of stock in the meantime) — surfaced in the
    -- review UI so the human can see why and retry/re-match, per-row,
    -- without the whole batch action failing silently.
    last_process_error       TEXT,

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_order_rows_have_quantity CHECK (row_type <> 'Order' OR quantity IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_ebay_sales_rows_batch_id ON ebay_sales_rows (batch_id);
CREATE INDEX IF NOT EXISTS ix_ebay_sales_rows_review_status ON ebay_sales_rows (review_status);

-- The real idempotency invariant for this milestone: re-uploading the same
-- file, or an overlapping month, must never create a second reviewable row
-- for the same real-world eBay order line item — enforced at the DATABASE
-- level (a real unique index), not just an app-level pre-check (see
-- CLAUDE.md's "genuine DB-level uniqueness constraint" requirement).
-- Partial (row_type = 'Order' only): Transaction ID is verified unique
-- among real Order line items, but NOT globally unique across every row
-- type (a Hold row can legitimately echo an unrelated Order row's
-- Transaction ID — see the ebay_transaction_id column comment above), so a
-- bare global unique index would be WRONG here, not just unnecessary.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ebay_sales_rows_order_txn_id
    ON ebay_sales_rows (ebay_transaction_id)
    WHERE row_type = 'Order' AND ebay_transaction_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- ebay_sales_row_depletions: links one ebay_sales_rows row to the real
-- depletion event(s) it produced. One-to-one for a fungible row (one
-- fungible_depletions row covers the whole line's quantity); one-to-many
-- for a serialized row with quantity > 1 (one serial_units row per unit,
-- since deplete_serial_unit() only ever marks ONE unit at a time).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ebay_sales_row_depletions (
    id                      SERIAL PRIMARY KEY,
    ebay_sales_row_id       INTEGER NOT NULL REFERENCES ebay_sales_rows(id) ON DELETE CASCADE,
    fungible_depletion_id   INTEGER REFERENCES fungible_depletions(id),
    serial_unit_id          INTEGER REFERENCES serial_units(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_exactly_one_depletion_ref CHECK (
        (fungible_depletion_id IS NOT NULL AND serial_unit_id IS NULL)
        OR (fungible_depletion_id IS NULL AND serial_unit_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_ebay_sales_row_depletions_row_id ON ebay_sales_row_depletions (ebay_sales_row_id);

-- ---------------------------------------------------------------------
-- Second, independent layer of the same idempotency invariant, this time
-- on the real leaf depletion tables themselves (Milestone 5's own
-- authoritative records) — mirrors this project's established two-layer
-- pattern (app-level pre-check + real DB-level backstop, e.g. Milestone
-- 2's SKU/serial uniqueness, Milestone 5's negative-stock trigger).
-- Belt-and-suspenders on top of the ebay_sales_rows-level index above: even
-- if some future code path called inventory.depletions functions directly
-- with a duplicate ebay_transaction_id, bypassing this milestone's review
-- queue entirely, it still could never double-post the same eBay
-- transaction into real inventory movement.
--
-- Nullable + partial (WHERE ebay_transaction_id IS NOT NULL) so ordinary,
-- non-eBay-sourced depletions (Purchase Entry/Item Detail's manual
-- "Mark as Sold", already shipped in Milestone 5) are entirely unaffected
-- — their ebay_transaction_id stays NULL and never participates in this
-- constraint.
-- ---------------------------------------------------------------------
ALTER TABLE fungible_depletions ADD COLUMN IF NOT EXISTS ebay_transaction_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_fungible_depletions_ebay_txn_id
    ON fungible_depletions (ebay_transaction_id)
    WHERE ebay_transaction_id IS NOT NULL;

-- serial_units needs a slightly different value shape: a single "Order"
-- row can cover quantity > 1 even for a serialized item (the CSV has no
-- per-unit granularity — see CLAUDE.md), which would deplete N different
-- serial_units rows from the SAME eBay Transaction ID. A bare
-- ebay_transaction_id value would collide across those N rows even though
-- each is a legitimate, distinct real-world unit — so the ingestion layer
-- (ingestion/ebay_import.py) suffixes each unit beyond the first
-- (`"{txn_id}-unit{n}"`) before calling deplete_serial_unit(), keeping
-- each value genuinely unique per physical unit while still tracing back
-- to the same eBay transaction by prefix.
ALTER TABLE serial_units ADD COLUMN IF NOT EXISTS ebay_transaction_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_serial_units_ebay_txn_id
    ON serial_units (ebay_transaction_id)
    WHERE ebay_transaction_id IS NOT NULL;
