-- Migration 002: incremental-export tracking for Milestone 4 (the
-- Noctrowl export pipeline).
--
-- Adds a nullable `exported_at` timestamp to `purchases`. NULL means "never
-- successfully exported"; a real value is the timestamp of the export run
-- that included this purchase in a CONFIRMED successful Drive upload.
--
-- Mirrors Project-Noctrowl's own review-queue "posted" marker (a
-- posted_at-style column set only after the corresponding action has
-- genuinely completed) so re-running the export script never re-sends a
-- purchase that was already sent — see export/runner.py, which only sets
-- this column after the Drive upload call has returned successfully, never
-- before and never speculatively.
ALTER TABLE purchases
    ADD COLUMN IF NOT EXISTS exported_at TIMESTAMPTZ;

-- Supports the incremental query (`WHERE exported_at IS NULL`) efficiently
-- as the purchases table grows. A plain (non-partial) index is enough at
-- this table's expected scale; not worth the extra DDL of a partial index
-- for a prototype-scale table.
CREATE INDEX IF NOT EXISTS ix_purchases_exported_at ON purchases (exported_at);
