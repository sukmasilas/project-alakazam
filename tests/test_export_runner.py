"""Tests for export.runner.run_export against a real Postgres connection
(the `conn` fixture — real purchases via save_purchase) with a STUBBED
Drive client (no real Google API / network access needed to run this
suite). Covers: nothing-to-export short-circuit, successful export marking
exported_at, incremental behavior across two runs, and — the core
data-safety guarantee — that a Drive failure never marks any row exported.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase

from export.runner import EXPORT_ROOT_FOLDER_NAME, run_export


def _new_fungible(name, category_code="AUTOMOTIVE"):
    return NewItemInput(name=name, category_code=category_code, identity_mode="fungible")


def _make_purchase(conn, vendor="Vendor", amount=100000, item_name="Widget"):
    purchase = PurchaseInput(
        purchase_date=date(2026, 6, 1),
        vendor_description=vendor,
        total_amount_paid=Decimal(amount),
        shipping_mode="none",
        lines=[
            PurchaseLineInput(
                new_item=_new_fungible(item_name),
                quantity=1,
                pricing_mode="direct",
                price_entry_mode="total",
                price_value=Decimal(amount),
            )
        ],
    )
    return save_purchase(conn, purchase)


class StubDriveClient:
    """Records every call it receives; never touches the network. Can be
    configured to raise on either method to exercise the failure path.
    """

    def __init__(self, *, fail_on_folder=False, fail_on_upload=False):
        self.fail_on_folder = fail_on_folder
        self.fail_on_upload = fail_on_upload
        self.folder_calls: list[str] = []
        self.upload_calls: list[tuple] = []
        self._folder_id_counter = 0

    def find_or_create_folder(self, name, parent_id=None):
        self.folder_calls.append(name)
        if self.fail_on_folder:
            raise RuntimeError("simulated Drive folder lookup/creation failure")
        return "fake-folder-id"

    def upload_file(self, parent_id, name, content, mime_type):
        self.upload_calls.append((parent_id, name, content, mime_type))
        if self.fail_on_upload:
            raise RuntimeError("simulated Drive upload failure")
        return f"fake-file-id-{len(self.upload_calls)}"


def _exported_at_values(conn, purchase_ids):
    rows = conn.execute(
        text("SELECT id, exported_at FROM purchases WHERE id = ANY(:ids)"),
        {"ids": purchase_ids},
    ).all()
    return {r[0]: r[1] for r in rows}


class TestNothingToExport:
    def test_empty_database_makes_no_drive_calls(self, conn):
        drive = StubDriveClient()
        result = run_export(conn, drive)

        assert result.ran_upload is False
        assert result.exported_purchase_ids == []
        assert drive.folder_calls == []
        assert drive.upload_calls == []

    def test_all_purchases_already_exported_makes_no_drive_calls(self, conn):
        saved = _make_purchase(conn)
        conn.execute(
            text("UPDATE purchases SET exported_at = now() WHERE id = :id"),
            {"id": saved.id},
        )

        drive = StubDriveClient()
        result = run_export(conn, drive)

        assert result.ran_upload is False
        assert drive.folder_calls == []
        assert drive.upload_calls == []


class TestSuccessfulExport:
    def test_marks_exported_and_uses_expected_folder_and_filename(self, conn):
        saved = _make_purchase(conn, vendor="Vendor A", amount=100000, item_name="Widget A")
        fixed_now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)

        drive = StubDriveClient()
        result = run_export(conn, drive, now=fixed_now)

        assert result.ran_upload is True
        assert result.exported_purchase_ids == [saved.id]
        assert result.row_count == 1
        assert result.filename == "alakazam-export-20260910T140000Z.csv"
        assert result.drive_folder_id == "fake-folder-id"
        assert result.drive_file_id == "fake-file-id-1"

        assert drive.folder_calls == [EXPORT_ROOT_FOLDER_NAME]
        assert len(drive.upload_calls) == 1
        parent_id, name, content, mime_type = drive.upload_calls[0]
        assert parent_id == "fake-folder-id"
        assert name == "alakazam-export-20260910T140000Z.csv"
        assert mime_type == "text/csv"
        assert b"Widget A" in content

        exported = _exported_at_values(conn, [saved.id])
        assert exported[saved.id] == fixed_now

    def test_uploaded_csv_content_matches_csv_builder_output(self, conn):
        saved = _make_purchase(conn, vendor="Vendor A", amount=250000, item_name="Widget A")

        drive = StubDriveClient()
        run_export(conn, drive)

        from export.csv_builder import build_export_csv

        # Re-derive the expected CSV independently (a second call, after
        # exported_at has been set) to confirm the uploaded bytes matched
        # what was queried for exactly these purchase ids at export time —
        # not a coincidentally-matching re-render of everything.
        expected_csv, _ = build_export_csv(conn, purchase_ids=[saved.id])
        _, _, uploaded_content, _ = drive.upload_calls[0]
        assert uploaded_content.decode("utf-8") == expected_csv


class TestIncrementalAcrossTwoRuns:
    def test_second_run_only_exports_new_purchases(self, conn):
        saved1 = _make_purchase(conn, vendor="Vendor A", amount=100000, item_name="Widget A")

        drive = StubDriveClient()
        result1 = run_export(conn, drive)
        assert result1.exported_purchase_ids == [saved1.id]

        # No new purchases yet — a second run right away must be a no-op.
        result_noop = run_export(conn, drive)
        assert result_noop.ran_upload is False
        assert len(drive.upload_calls) == 1  # unchanged from the first run

        saved2 = _make_purchase(conn, vendor="Vendor B", amount=200000, item_name="Widget B")
        result2 = run_export(conn, drive)

        assert result2.ran_upload is True
        assert result2.exported_purchase_ids == [saved2.id]
        assert result2.row_count == 1
        assert len(drive.upload_calls) == 2

        # The first purchase's exported_at must be untouched by the second run.
        exported = _exported_at_values(conn, [saved1.id, saved2.id])
        assert exported[saved1.id] is not None
        assert exported[saved2.id] is not None
        assert exported[saved1.id] != exported[saved2.id]


class TestDriveFailureNeverMarksExported:
    def test_upload_failure_leaves_exported_at_null(self, conn):
        saved = _make_purchase(conn)
        drive = StubDriveClient(fail_on_upload=True)

        with pytest.raises(RuntimeError, match="simulated Drive upload failure"):
            run_export(conn, drive)

        exported = _exported_at_values(conn, [saved.id])
        assert exported[saved.id] is None

        # A retry with a working client should pick up exactly this purchase.
        drive_retry = StubDriveClient()
        result = run_export(conn, drive_retry)
        assert result.exported_purchase_ids == [saved.id]

    def test_folder_creation_failure_leaves_exported_at_null(self, conn):
        saved = _make_purchase(conn)
        drive = StubDriveClient(fail_on_folder=True)

        with pytest.raises(RuntimeError, match="simulated Drive folder lookup/creation failure"):
            run_export(conn, drive)

        assert drive.upload_calls == []  # never reached the upload step
        exported = _exported_at_values(conn, [saved.id])
        assert exported[saved.id] is None

    def test_failure_leaves_no_partial_marking_across_multiple_purchases(self, conn):
        saved1 = _make_purchase(conn, vendor="Vendor A", item_name="Widget A")
        saved2 = _make_purchase(conn, vendor="Vendor B", item_name="Widget B")
        drive = StubDriveClient(fail_on_upload=True)

        with pytest.raises(RuntimeError):
            run_export(conn, drive)

        exported = _exported_at_values(conn, [saved1.id, saved2.id])
        assert exported[saved1.id] is None
        assert exported[saved2.id] is None
