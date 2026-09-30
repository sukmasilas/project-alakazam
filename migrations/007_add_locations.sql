-- Migration 007: shelving/location tracking, confirmed 2026-10-01.
--
-- Records where each item is physically stored. See CLAUDE.md's "Shelving/
-- location feature, confirmed 2026-10-01" for the full rationale:
--   - One location PER ITEM (not per-serialized-unit, not split across
--     multiple locations for fungible stock) — the simplest model.
--   - A structured, admin-editable list of locations, not free text —
--     mirrors migration 006's ``consignors`` table shape, except ``name``
--     is UNIQUE here (a location is a fixed physical slot; unlike a
--     consignor's name, two rows meaning the same real place would be a
--     real data-quality bug, not a legitimate case like two different
--     consignors happening to share a name).
--   - Not a money- or stock-correctness field — no DB trigger/invariant
--     needed the way migration 006's consignor_id needed one. A plain
--     nullable FK is sufficient.
--
-- items.location_id is nullable (existing items predate this feature, and a
-- user may legitimately skip setting one) and, unlike consignor_id, is NOT
-- write-once — inventory/locations.py::set_item_location() updates it
-- freely any time an item physically moves (see CLAUDE.md: "editable
-- afterward from Item Detail").
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS locations (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_locations_name ON locations (name);

ALTER TABLE items ADD COLUMN IF NOT EXISTS location_id INTEGER REFERENCES locations(id);
CREATE INDEX IF NOT EXISTS ix_items_location_id ON items (location_id);
