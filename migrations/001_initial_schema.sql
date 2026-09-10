-- Migration 001: initial schema for Project-Alakazam, Milestone 2.
--
-- Scope AT THE TIME THIS MIGRATION WAS WRITTEN: categories, items,
-- serial_units, purchases, purchase_line_items — the core acquisition-only
-- inventory data model described in docs/design/schema-design.md. No web
-- app, no ingestion, no sale/depletion tables yet (see CLAUDE.md's Build
-- status & decisions). This acquisition-only framing is historical, not
-- current — see migration 003 (Milestone 5), which adds real sale-side
-- depletion on top of the tables below without altering them.
--
-- Migration approach (see schema-design.md "Migrations" section for the
-- full reasoning): plain, numbered, idempotent .sql files, applied in order
-- by scripts/run_migrations.py, tracked in schema_migrations. Chosen over a
-- heavier tool (Alembic) because this is a from-scratch schema with no
-- migration history to reconcile yet, and over bare
-- SQLAlchemy-Core-metadata.create_all() (Project-Noctrowl's own approach)
-- because two of this schema's invariants need genuine trigger DDL that
-- reads more clearly as plain SQL than as SQLAlchemy DDL constructs.

-- ---------------------------------------------------------------------
-- categories: 5 confirmed categories, deliberately a data table (not a
-- CHECK-constraint enum) so a 6th category is just an INSERT, not a schema
-- change — CLAUDE.md's brief says "extensible".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    id              SERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- e.g. 'TCG', 'WATCHES'
    name            TEXT NOT NULL,             -- display name, e.g. 'Toys & Collectibles'
    sku_prefix      TEXT NOT NULL UNIQUE,      -- e.g. 'TCG', 'WATCH', 'AUTO', 'TOY', 'OTH'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- items: the SKU catalog. identity_mode is a property of the ITEM, never
-- inferred from category (Automotive deliberately mixes both per the
-- design doc).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id              SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL,
    name            TEXT NOT NULL,
    category_id     INTEGER NOT NULL REFERENCES categories(id),
    identity_mode   TEXT NOT NULL CHECK (identity_mode IN ('fungible', 'serialized')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Real uniqueness enforcement at the DB level (see CLAUDE.md brief — the
-- mockup's bug was relying on the generator alone). Case-sensitive as
-- stored; application code normalizes to uppercase before insert/lookup
-- (see inventory/uniqueness.py) so this index is the real backstop against
-- both a generation collision and a manually-typed duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_sku ON items (sku);

-- ---------------------------------------------------------------------
-- purchases: the header. date is the BOOKING point (on-hand stock/cost
-- books at time of payment, not physical receipt — matches Project-
-- Noctrowl's own COGS timing convention).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchases (
    id                          SERIAL PRIMARY KEY,
    purchase_ref                TEXT NOT NULL UNIQUE,   -- e.g. 'PUR-2026-0102'
    purchase_date               DATE NOT NULL,
    vendor_description          TEXT NOT NULL,
    total_amount_paid           NUMERIC(20, 2) NOT NULL CHECK (total_amount_paid >= 0),
    currency                    TEXT NOT NULL DEFAULT 'IDR',
    -- Nullable FX rate groundwork for a future non-IDR purchase. Not
    -- exercised by this milestone's math — every real/test purchase so far
    -- is IDR, same as the mockup (Currency field only ever offers "IDR").
    fx_rate_to_idr              NUMERIC(18, 6),

    shipping_mode               TEXT NOT NULL CHECK (shipping_mode IN ('none', 'manual', 'pooled')),
    pooled_shipping_total       NUMERIC(20, 2) CHECK (pooled_shipping_total IS NULL OR pooled_shipping_total >= 0),
    pooled_shipping_method      TEXT CHECK (pooled_shipping_method IN ('equal', 'by_weight', 'by_value')),

    -- Lump-sum fallback: scoped to exactly ONE group per purchase (design
    -- doc Open Question 8) — a purchase-level flag/total/method, never a
    -- per-line lump-sum group id.
    lump_sum_active             BOOLEAN NOT NULL DEFAULT false,
    lump_sum_total              NUMERIC(20, 2) CHECK (lump_sum_total IS NULL OR lump_sum_total >= 0),
    lump_sum_method             TEXT CHECK (lump_sum_method IN ('equal', 'by_weight', 'by_value')),

    -- Invoice/proof-of-transfer groundwork (Milestone 1's confirmed
    -- near-term feature) — no real Drive/OCR integration yet, just the
    -- fields so a later milestone is additive.
    invoice_document_ref        TEXT,
    invoice_ocr_status          TEXT CHECK (invoice_ocr_status IN ('parsed', 'needs_review')),
    invoice_parsed_fields       JSONB,

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_pooled_shipping_fields CHECK (
        (shipping_mode = 'pooled' AND pooled_shipping_method IS NOT NULL)
        OR (shipping_mode <> 'pooled' AND pooled_shipping_total IS NULL AND pooled_shipping_method IS NULL)
    ),
    CONSTRAINT chk_lump_sum_fields CHECK (
        (lump_sum_active AND lump_sum_total IS NOT NULL AND lump_sum_method IS NOT NULL)
        OR (NOT lump_sum_active AND lump_sum_total IS NULL AND lump_sum_method IS NULL)
    )
);

-- ---------------------------------------------------------------------
-- purchase_line_items
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchase_line_items (
    id                      SERIAL PRIMARY KEY,
    purchase_id             INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    item_id                 INTEGER NOT NULL REFERENCES items(id),
    quantity                INTEGER NOT NULL CHECK (quantity > 0),

    pricing_mode            TEXT NOT NULL CHECK (pricing_mode IN ('direct', 'lumpsum_group')),
    -- direct-entry supporting fields
    price_entry_mode        TEXT CHECK (price_entry_mode IN ('per_unit', 'total')),
    price_value             NUMERIC(20, 2),
    -- lump-sum-group supporting inputs (only the field matching the
    -- purchase's lump_sum_method is ever populated for a given line)
    lumpsum_weight_kg       NUMERIC(14, 3),
    lumpsum_value           NUMERIC(20, 2),

    -- shipping: independent of item pricing entirely (see design doc)
    ships_separately        BOOLEAN NOT NULL DEFAULT false,
    manual_shipping_amount  NUMERIC(20, 2),
    shipping_weight_kg      NUMERIC(14, 3),

    -- final computed figures — these three are what the reconciliation
    -- invariant sums across every line in the purchase.
    allocated_item_cost     NUMERIC(20, 2) NOT NULL,
    shipping_share          NUMERIC(20, 2) NOT NULL,
    line_total              NUMERIC(20, 2) NOT NULL,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_line_total_is_sum CHECK (line_total = allocated_item_cost + shipping_share),
    CONSTRAINT chk_pricing_mode_fields CHECK (
        (pricing_mode = 'direct' AND price_entry_mode IS NOT NULL AND price_value IS NOT NULL)
        OR (pricing_mode = 'lumpsum_group' AND price_entry_mode IS NULL AND price_value IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_purchase_line_items_purchase_id ON purchase_line_items (purchase_id);
CREATE INDEX IF NOT EXISTS ix_purchase_line_items_item_id ON purchase_line_items (item_id);

-- ---------------------------------------------------------------------
-- serial_units: one row per physical unit, for serialized items only.
-- status is always 'on_hand' in this build (acquisition-only, no sale-side
-- depletion) — modeled as a real column now so a future depletion
-- milestone widens a CHECK constraint rather than needing a new column.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS serial_units (
    id                      SERIAL PRIMARY KEY,
    item_id                 INTEGER NOT NULL REFERENCES items(id),
    serial_id               TEXT NOT NULL,
    acquired_cost           NUMERIC(20, 2) NOT NULL CHECK (acquired_cost >= 0),
    purchase_line_item_id   INTEGER NOT NULL REFERENCES purchase_line_items(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'on_hand' CHECK (status IN ('on_hand')),
    -- Groundwork only (Milestone 1: real per-unit photo capture is a
    -- confirmed near-term feature, simulated client-side in the mockup).
    -- No real file storage built in this milestone.
    photo_reference         TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_serial_units_serial_id ON serial_units (serial_id);
CREATE INDEX IF NOT EXISTS ix_serial_units_item_id ON serial_units (item_id);
CREATE INDEX IF NOT EXISTS ix_serial_units_purchase_line_item_id ON serial_units (purchase_line_item_id);

-- ---------------------------------------------------------------------
-- Reconciliation invariant, enforced at the DATABASE level as a real,
-- structural backstop — not just an application-level check before insert
-- (see CLAUDE.md brief: "same debits = credits discipline as Noctrowl's
-- ledger"). Deferred to COMMIT time (DEFERRABLE INITIALLY DEFERRED) so the
-- app can insert a purchase header, then its line items one at a time,
-- inside one transaction, without the trigger firing prematurely after
-- the first line.
--
-- Mirrors Project-Noctrowl's own trg_check_journal_entry_balance pattern
-- (ledger/schema.py in the sibling project) — same idea, applied to this
-- project's own invariant.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_purchase_reconciliation() RETURNS trigger AS $$
DECLARE
    v_purchase_id   INTEGER;
    v_total_paid    NUMERIC(20, 2);
    v_line_sum      NUMERIC(20, 2);
    v_line_count    INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_purchase_id := OLD.purchase_id;
    ELSE
        v_purchase_id := NEW.purchase_id;
    END IF;

    SELECT total_amount_paid INTO v_total_paid FROM purchases WHERE id = v_purchase_id;
    IF v_total_paid IS NULL THEN
        -- The purchase row itself no longer exists (e.g. the whole
        -- purchase, including its lines via ON DELETE CASCADE, was
        -- deleted in this same transaction) — nothing left to reconcile.
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(allocated_item_cost + shipping_share), 0), COUNT(*)
      INTO v_line_sum, v_line_count
      FROM purchase_line_items
      WHERE purchase_id = v_purchase_id;

    IF v_line_sum <> v_total_paid THEN
        RAISE EXCEPTION
            'Purchase % is out of balance: line items sum to % but total_amount_paid is %',
            v_purchase_id, v_line_sum, v_total_paid;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_purchase_reconciliation ON purchase_line_items;
CREATE CONSTRAINT TRIGGER trg_check_purchase_reconciliation
    AFTER INSERT OR UPDATE OR DELETE ON purchase_line_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_purchase_reconciliation();

-- A purchase with zero line items would never fire the trigger above (it
-- only fires on a purchase_line_items row event). This second trigger
-- closes that gap: at commit time, every purchase row must have at least
-- one line item.
CREATE OR REPLACE FUNCTION check_purchase_has_lines() RETURNS trigger AS $$
DECLARE
    v_line_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_line_count FROM purchase_line_items WHERE purchase_id = NEW.id;
    IF v_line_count = 0 THEN
        RAISE EXCEPTION 'Purchase % has no line items — a purchase cannot be saved with zero lines', NEW.id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_purchase_has_lines ON purchases;
CREATE CONSTRAINT TRIGGER trg_check_purchase_has_lines
    AFTER INSERT ON purchases
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_purchase_has_lines();
