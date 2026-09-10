"""eBay sales CSV ingestion / review-queue matching — Milestone 6.

See CLAUDE.md's "Milestone 6 scope decisions, confirmed 2026-09-11" for the
full rationale. This is a review-queue import, not an auto-matcher: no row
ever auto-posts a depletion without explicit human confirmation, no matter
how confident a title-match suggestion looks.
"""
