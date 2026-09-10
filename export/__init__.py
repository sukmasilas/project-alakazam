"""Milestone 4: the Noctrowl export pipeline.

Reads already-computed purchase data out of the Milestone 2/3 schema and
hands it to Project-Noctrowl as a CSV file via Google Drive — a one-way,
manual-trigger, incremental file handoff (see CLAUDE.md's "Milestone 4
decisions, confirmed 2026-09-10"). This package never computes money; it
only reads and formats values ``inventory/allocation.py`` and
``inventory/purchases.py`` already computed and stored.

Modules:
- ``csv_builder``: pure(-ish) row-shaping + CSV-text generation from a DB
  connection. No Drive/network code.
- ``drive_client``: a thin, OAuth-only Google Drive API v3 wrapper, with
  Alakazam's own separate credentials (never Noctrowl's).
- ``runner``: the orchestrator — queries un-exported purchases, builds the
  CSV, uploads it, and marks `exported_at` only after a confirmed
  successful upload.
"""
