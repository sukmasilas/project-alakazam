-- Migration 006: consignment tracking — Milestone 8.
--
-- See CLAUDE.md's "Milestone 8 decisions, confirmed 2026-09-15" for the
-- full rationale. Summary: items owned by a third-party consignor,
-- physically held by the seller, never the seller's own asset. Serialized
-- only in this first pass (no fungible consignment) — a consigned unit's
-- own direct cost is genuinely Rp 0 (the seller never bought it), which
-- sidesteps weighted-average pooling entirely. No sale price/revenue field
-- anywhere here — same standing rule as every prior milestone; Alakazam
-- tracks inventory/cost movement (and, new this milestone, a paid/unpaid
-- reimbursement STATUS) only, never a payout Rupiah amount (that formula
-- lives entirely on Project-Noctrowl's side).

-- ---------------------------------------------------------------------
-- consignors: kept deliberately simple (CLAUDE.md: "not meant to be a full
-- CRM") — a name and a free-text contact field. A separate system (per
-- Noctrowl's own CLAUDE.md) handles consignor-level relationship/
-- operational detail beyond this.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consignors (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    contact_info    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- items.consignor_id: nullable FK — an item is EITHER a normal (stock/
-- pre-order) item Alakazam's earlier milestones already model, OR a
-- consigned item tagged to exactly one consignor (item-separation rule:
-- a consigned item always gets its own distinct item/SKU, never shared
-- with owned stock of a nominally-similar product — see CLAUDE.md).
-- consignor_id is set once, at item creation
-- (inventory/consignment.py::intake_consigned_units), and never updated
-- afterward by any code in this project — same write-once treatment as
-- identity_mode (migration 005's own comment already confirmed, by
-- grepping for any `UPDATE items SET identity_mode`, that no code mutates
-- that column either).
-- ---------------------------------------------------------------------
ALTER TABLE items ADD COLUMN IF NOT EXISTS consignor_id INTEGER REFERENCES consignors(id);
CREATE INDEX IF NOT EXISTS ix_items_consignor_id ON items (consignor_id);

-- ---------------------------------------------------------------------
-- Fungible-consignment-is-out-of-scope invariant, enforced at the DATABASE
-- level as a real backstop — not just the application-level pre-check in
-- inventory/consignment.py — same two-layer discipline as every other
-- cross-table invariant in this project (migration 001's reconciliation
-- trigger, migration 003's negative-stock trigger, migration 005's
-- fungible-only-for-preorder trigger). No advisory lock needed here, for
-- the same reason migration 005's trigger needed none: consignor_id (like
-- identity_mode) is write-once, so there is no concurrent-mutation race to
-- guard against — this is a plain cross-table/cross-column validation.
-- Fires on INSERT and UPDATE (not just INSERT) as genuine belt-and-
-- suspenders: nothing in this codebase updates consignor_id/identity_mode
-- today, but if that ever changed, this trigger still catches an invalid
-- combination rather than silently allowing it.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_item_consignor_requires_serialized() RETURNS trigger AS $$
BEGIN
    IF NEW.consignor_id IS NOT NULL AND NEW.identity_mode <> 'serialized' THEN
        RAISE EXCEPTION
            'Item % has consignor_id % set but identity_mode=% — consigned items must be serialized (Milestone 8: no fungible consignment support)',
            NEW.id, NEW.consignor_id, NEW.identity_mode
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_item_consignor_requires_serialized ON items;
CREATE TRIGGER trg_check_item_consignor_requires_serialized
    AFTER INSERT OR UPDATE ON items
    FOR EACH ROW EXECUTE FUNCTION check_item_consignor_requires_serialized();

-- ---------------------------------------------------------------------
-- serial_units.purchase_line_item_id widens to NULLABLE.
--
-- A consigned unit is never tied to a real Purchase — intake is NOT a
-- Purchase, no money changes hands, inventory/purchases.py::save_purchase()
-- is not involved at all (see CLAUDE.md and inventory/consignment.py) — so
-- there is no purchase_line_items row for it to reference. Every
-- NON-consigned serialized unit (migration 001's original acquisition-only
-- design) still always traces back to a real purchase line, unchanged.
-- ---------------------------------------------------------------------
ALTER TABLE serial_units ALTER COLUMN purchase_line_item_id DROP NOT NULL;

-- ---------------------------------------------------------------------
-- The consignment/purchase-traceability consistency invariant, enforced at
-- the DATABASE level — a serialized unit's purchase_line_item_id must be
-- NULL if and only if its item is consigned, and a consigned unit's
-- acquired_cost must be exactly zero. Same two-layer discipline as every
-- other cross-table invariant in this project; no advisory lock needed for
-- the same reason as the trigger above (nothing here is a running-total
-- race — it's a plain per-row consistency check against the parent item's
-- already-immutable consignor_id).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_serial_unit_consignment_consistency() RETURNS trigger AS $$
DECLARE
    v_item_consignor_id INTEGER;
BEGIN
    SELECT consignor_id INTO v_item_consignor_id FROM items WHERE id = NEW.item_id;

    IF v_item_consignor_id IS NOT NULL THEN
        -- Consigned unit: never tied to a Purchase, and acquired at zero
        -- cost — correct, not a bug (see module comment / CLAUDE.md).
        IF NEW.purchase_line_item_id IS NOT NULL THEN
            RAISE EXCEPTION
                'serial_units.id % belongs to a consigned item (item %) but has a purchase_line_item_id set — a consigned unit must never be tied to a Purchase',
                NEW.id, NEW.item_id
                USING ERRCODE = '23514';
        END IF;
        IF NEW.acquired_cost <> 0 THEN
            RAISE EXCEPTION
                'serial_units.id % belongs to a consigned item (item %) but acquired_cost is % — a consigned unit must be acquired at zero cost',
                NEW.id, NEW.item_id, NEW.acquired_cost
                USING ERRCODE = '23514';
        END IF;
    ELSE
        -- Ordinary (non-consigned) unit: must always trace back to a real
        -- Purchase — the original migration 001 invariant, now enforced
        -- explicitly here since the NOT NULL constraint alone no longer
        -- guards it.
        IF NEW.purchase_line_item_id IS NULL THEN
            RAISE EXCEPTION
                'serial_units.id % belongs to a non-consigned item (item %) but has no purchase_line_item_id — every non-consigned unit must trace back to a real Purchase',
                NEW.id, NEW.item_id
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_serial_unit_consignment_consistency ON serial_units;
CREATE TRIGGER trg_check_serial_unit_consignment_consistency
    AFTER INSERT OR UPDATE ON serial_units
    FOR EACH ROW EXECUTE FUNCTION check_serial_unit_consignment_consistency();

-- ---------------------------------------------------------------------
-- consignor_reimbursements: a paid/unpaid STATUS per sold consigned unit
-- (CLAUDE.md: "a real scope addition beyond pure inventory movement... a
-- status flag only... never an amount"). One row per sold consigned unit —
-- serial_unit_id is UNIQUE, a real DB-level backstop (belt-and-suspenders
-- on top of inventory/consignment.py::sell_consigned_unit() only ever being
-- called once per unit, since deplete_serial_unit()'s own atomic
-- on_hand -> sold compare-and-swap already ensures a unit can only be sold
-- once) so this table can never end up with two reimbursement records for
-- the same physical sale.
--
-- status: unpaid -> paid, via inventory.consignment.mark_reimbursement_paid()
-- — a plain atomic `UPDATE ... WHERE status = 'unpaid'` compare-and-swap
-- (see that function), same pattern already proven safe in Milestones 5-7,
-- NEVER a trigger-based `SELECT ... FOR UPDATE`. 'paid' is TERMINAL in this
-- milestone — same "no correction/reversal flow yet" policy already
-- carried through every prior milestone (Purchases, Depletions, eBay
-- import, Pre-order sales).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consignor_reimbursements (
    id              SERIAL PRIMARY KEY,
    serial_unit_id  INTEGER NOT NULL UNIQUE REFERENCES serial_units(id),
    consignor_id    INTEGER NOT NULL REFERENCES consignors(id),
    status          TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'paid')),
    paid_date       DATE,
    -- Free-text traceability field only (e.g. a manually-typed bank
    -- transfer note) — never an amount. See module comment above.
    reference       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at         TIMESTAMPTZ,

    -- Keeps the terminal paid_date/paid_at from ever drifting out of sync
    -- with status — same discipline as migration 003's
    -- chk_serial_units_sold_date_matches_status and migration 005's
    -- chk_preorder_sales_terminal_timestamp_matches_status.
    CONSTRAINT chk_consignor_reimbursements_paid_fields_match_status CHECK (
        (status = 'unpaid' AND paid_date IS NULL AND paid_at IS NULL)
        OR (status = 'paid' AND paid_date IS NOT NULL AND paid_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_consignor_reimbursements_consignor_id ON consignor_reimbursements (consignor_id);
CREATE INDEX IF NOT EXISTS ix_consignor_reimbursements_status ON consignor_reimbursements (status);
