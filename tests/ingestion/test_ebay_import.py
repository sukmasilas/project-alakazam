"""Integration tests for ingestion/ebay_import.py against a real Postgres
database — the idempotency invariant is enforced partly by real DB unique
indexes (migrations/004_add_ebay_sales_import.sql), so these need the real
engine, same reasoning as tests/test_depletions.py.
"""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ingestion.ebay_import import (
    InvalidRowActionError,
    ProcessResult,
    RowNotFoundError,
    get_on_hand_serials_for_sku,
    import_csv,
    list_batches,
    list_review_rows,
    mark_row_matched,
    mark_row_skipped,
    process_confirmed_rows,
)
from inventory.exceptions import DuplicateEbaySaleError
from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase
from inventory.queries import get_item_stats

_HEADER = (
    "Transaction creation date,Type,Order number,Legacy order ID,Buyer username,Buyer name,"
    "Ship to city,Ship to province/region/state,Ship to zip,Ship to country,Net amount,"
    "Payout currency,Payout date,Payout ID,Payout method,Payout status,Reason for hold,"
    "Item ID,Transaction ID,Item title,Custom label,Quantity,Item subtotal,Shipping and handling,"
    "Seller collected tax,eBay collected tax,Final Value Fee - fixed,Final Value Fee - variable,"
    "Regulatory operating fee,Very high fee,Below standard performance fee,International fee,"
    "Charity donation,Deposit processing fee,Gross transaction amount,Transaction currency,"
    "Exchange rate,Reference ID,Description\n"
)


def _csv(rows: list[str], seller: str = "test.seller") -> str:
    preamble = "\n".join([f"junk preamble {i}" for i in range(9)]) + f"\nSeller,{seller}\n"
    return preamble + _HEADER + "\n".join(rows) + "\n"


def _order_row(order_no, txn_id, title, qty=1, custom_label="--", tx_date="Aug 15, 2026"):
    return (
        f'"{tx_date}",Order,{order_no},{order_no},buyer,Buyer,City,ST,0,US,10,USD,'
        f"--,--,--,--,--,90000{order_no[-3:]},{txn_id},{title},{custom_label},{qty},"
        f"10,0,--,--,--,-1,--,--,--,-0.1,--,--,10,USD,--,--,--"
    )


def _refund_row(order_no, title, tx_date="Aug 16, 2026"):
    return (
        f'"{tx_date}",Refund,{order_no},{order_no},buyer,Buyer,City,ST,0,US,-10,USD,'
        f"--,--,--,--,--,--,--,{title},--,--,--,--,--,-1,0.4,2,--,--,--,0.2,--,--,-10,USD,--,Cancel,--"
    )


def _buy_fungible(conn, name, quantity, price_value, category_code="AUTOMOTIVE"):
    result = save_purchase(
        conn,
        PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description=f"Stock-up: {name}",
            total_amount_paid=Decimal(price_value) * quantity,
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=NewItemInput(name=name, category_code=category_code, identity_mode="fungible"),
                    quantity=quantity,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal(price_value),
                )
            ],
        ),
    )
    return result.lines[0].sku


def _buy_serialized(conn, name, quantity, total_price, category_code="WATCHES"):
    result = save_purchase(
        conn,
        PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description=f"Stock-up: {name}",
            total_amount_paid=Decimal(total_price),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=NewItemInput(name=name, category_code=category_code, identity_mode="serialized"),
                    quantity=quantity,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal(total_price),
                )
            ],
        ),
    )
    return result.lines[0].sku, result.lines[0].serial_ids


class TestImportCsv:
    def test_stores_order_and_refund_rows_counts_everything_else(self, conn):
        csv_text = _csv(
            [
                _order_row("11-00000-00001", "9000000001", "Widget A"),
                _order_row("11-00000-00002", "9000000002", "Widget B", qty=2),
                _refund_row("11-00000-00003", "Widget C"),
            ]
        )
        summary = import_csv(conn, "test1.csv", csv_text)
        assert summary.order_rows_stored == 2
        assert summary.refund_rows_stored == 1
        assert summary.order_rows_duplicate == 0

        rows = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)
        assert len(rows) == 3
        assert {r.review_status for r in rows if r.row_type == "Order"} == {"pending"}

    def test_reimporting_the_same_file_never_double_stores_rows(self, conn):
        csv_text = _csv([_order_row("11-00000-00001", "9000000001", "Widget A")])
        first = import_csv(conn, "test1.csv", csv_text)
        second = import_csv(conn, "test1.csv", csv_text)

        assert first.order_rows_stored == 1
        assert second.order_rows_stored == 0
        assert second.order_rows_duplicate == 1

        count = conn.execute(
            text("SELECT COUNT(*) FROM ebay_sales_rows WHERE ebay_transaction_id = '9000000001'")
        ).scalar_one()
        assert count == 1

    def test_overlapping_month_reimport_of_the_same_transaction_is_deduped(self, conn):
        """The real scenario CLAUDE.md calls out: two different months'
        files that happen to share a few days of data must not
        double-import the shared rows.
        """
        shared_row = _order_row("11-00000-00001", "9000000001", "Widget A", tx_date="Jul 31, 2026")
        july = _csv([shared_row, _order_row("11-00000-00002", "9000000002", "Widget B", tx_date="Jul 30, 2026")])
        august = _csv([shared_row, _order_row("11-00000-00003", "9000000003", "Widget C", tx_date="Aug 1, 2026")])

        july_summary = import_csv(conn, "july.csv", july)
        august_summary = import_csv(conn, "august.csv", august)

        assert july_summary.order_rows_stored == 2
        assert august_summary.order_rows_stored == 1  # only Widget C is new
        assert august_summary.order_rows_duplicate == 1  # the shared row

        count = conn.execute(
            text("SELECT COUNT(*) FROM ebay_sales_rows WHERE ebay_transaction_id = '9000000001'")
        ).scalar_one()
        assert count == 1

    def test_order_row_missing_transaction_id_is_not_imported(self, conn):
        weird = (
            '"Aug 31, 2026",Order,30-00000-00001,30-00000-00001,b,B,City,ST,0,US,--,--,'
            "--,--,--,--,--,900000002,--,Weird Item No Txn Id,--,1,5,0,--,--,--,-1,--,--,--,-0.1,--,--,--,--,--,--,--"
        )
        summary = import_csv(conn, "weird.csv", _csv([weird]))
        assert summary.order_rows_stored == 0
        assert summary.order_rows_missing_transaction_id == 1
        rows = list_review_rows(conn, include_suggestions=False)
        assert rows == []

    def test_raw_sql_bypass_of_the_unique_index_is_rejected(self, conn):
        """Proves the real DB-level unique index is the actual backstop,
        independent of import_csv()'s own app-level duplicate handling.
        """
        summary = import_csv(conn, "test1.csv", _csv([_order_row("11-00000-00001", "9000000001", "Widget A")]))
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO ebay_sales_rows (batch_id, row_type, item_title, ebay_transaction_id, quantity) "
                        "VALUES (:batch_id, 'Order', 'Widget A Duplicate', '9000000001', 1)"
                    ),
                    {"batch_id": summary.batch_id},
                )
        # Connection still usable (savepoint absorbed the abort).
        count = conn.execute(text("SELECT COUNT(*) FROM ebay_sales_rows")).scalar_one()
        assert count == 1

    def test_batches_are_listed_newest_first_with_real_counts(self, conn):
        import_csv(conn, "a.csv", _csv([_order_row("11-00000-00001", "9000000001", "A")]))
        import_csv(conn, "b.csv", _csv([_order_row("11-00000-00002", "9000000002", "B")]))
        batches = list_batches(conn)
        assert [b["source_filename"] for b in batches] == ["b.csv", "a.csv"]
        assert batches[0]["order_rows_stored"] == 1


class TestMatchAndSkip:
    def test_match_fungible_row_stages_without_posting(self, conn):
        sku = _buy_fungible(conn, "Brake Pad Set", quantity=10, price_value=100_000)
        summary = import_csv(conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Brake Pad Set")]))
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]

        updated = mark_row_matched(conn, row.id, sku)
        assert updated.review_status == "matched"
        assert updated.matched_item_sku == sku

        # Nothing posted yet — stock unchanged.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 10

    def test_match_serialized_row_requires_exact_unit_count(self, conn):
        sku, serials = _buy_serialized(conn, "Rolex Submariner", quantity=3, total_price=30_000_000)
        summary = import_csv(
            conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Rolex Submariner", qty=2)])
        )
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]

        with pytest.raises(InvalidRowActionError):
            mark_row_matched(conn, row.id, sku, serial_ids=[serials[0]])  # only 1, need 2

        updated = mark_row_matched(conn, row.id, sku, serial_ids=[serials[0], serials[1]])
        assert updated.review_status == "matched"
        assert set(updated.matched_serial_ids) == {serials[0], serials[1]}

    def test_match_serialized_row_rejects_duplicate_unit_picks(self, conn):
        sku, serials = _buy_serialized(conn, "Omega Speedmaster", quantity=2, total_price=20_000_000)
        summary = import_csv(
            conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Omega Speedmaster", qty=2)])
        )
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        with pytest.raises(InvalidRowActionError):
            mark_row_matched(conn, row.id, sku, serial_ids=[serials[0], serials[0]])

    def test_refund_row_cannot_be_matched_or_skipped(self, conn):
        summary = import_csv(conn, "t.csv", _csv([_refund_row("11-00000-00001", "Widget A")]))
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        with pytest.raises(InvalidRowActionError):
            mark_row_matched(conn, row.id, "AUTO-WIDGET-0001")
        with pytest.raises(InvalidRowActionError):
            mark_row_skipped(conn, row.id)

    def test_skip_then_rematch_is_allowed(self, conn):
        sku = _buy_fungible(conn, "Oil Filter", quantity=5, price_value=50_000)
        summary = import_csv(conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Oil Filter")]))
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]

        skipped = mark_row_skipped(conn, row.id)
        assert skipped.review_status == "skipped"

        rematched = mark_row_matched(conn, row.id, sku)
        assert rematched.review_status == "matched"

    def test_unknown_row_id_raises_not_found(self, conn):
        with pytest.raises(RowNotFoundError):
            mark_row_matched(conn, 999999, "AUTO-NOPE-0001")
        with pytest.raises(RowNotFoundError):
            mark_row_skipped(conn, 999999)


class TestProcessConfirmedRows:
    def test_processes_a_matched_fungible_row_through_the_real_engine(self, conn):
        sku = _buy_fungible(conn, "Spark Plug", quantity=20, price_value=25_000)
        summary = import_csv(conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Spark Plug", qty=5)]))
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        mark_row_matched(conn, row.id, sku)

        results = process_confirmed_rows(conn, batch_id=summary.batch_id)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].fungible_depletion_id is not None

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 15

        posted_row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        assert posted_row.review_status == "posted"

        dep = conn.execute(
            text("SELECT ebay_transaction_id, reference FROM fungible_depletions")
        ).mappings().first()
        assert dep["ebay_transaction_id"] == "9000000001"
        assert "9000000001" in dep["reference"]

    def test_processes_a_matched_serialized_row_with_multiple_units(self, conn):
        sku, serials = _buy_serialized(conn, "Rolex Submariner", quantity=3, total_price=30_000_000)
        summary = import_csv(
            conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Rolex Submariner", qty=2)])
        )
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        mark_row_matched(conn, row.id, sku, serial_ids=[serials[0], serials[1]])

        results = process_confirmed_rows(conn, batch_id=summary.batch_id)
        assert results[0].success is True
        assert set(results[0].serial_ids_depleted) == {serials[0], serials[1]}

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 1

        txn_ids = {
            r[0]
            for r in conn.execute(
                text("SELECT ebay_transaction_id FROM serial_units WHERE status = 'sold'")
            ).all()
        }
        # Suffixed per-unit (see migrations/004 comment) — never a bare
        # duplicate value across the two units of the same eBay row.
        assert txn_ids == {"9000000001-unit1", "9000000001-unit2"}

    def test_a_failing_row_does_not_block_other_rows_in_the_same_batch(self, conn):
        sku_ok = _buy_fungible(conn, "Timing Belt", quantity=10, price_value=200_000)
        sku_short = _buy_fungible(conn, "Turbo Kit", quantity=1, price_value=500_000)

        summary = import_csv(
            conn,
            "t.csv",
            _csv(
                [
                    _order_row("11-00000-00001", "9000000001", "Timing Belt", qty=3),
                    _order_row("11-00000-00002", "9000000002", "Turbo Kit", qty=5),  # more than the 1 on hand
                ]
            ),
        )
        rows = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)
        row_ok = next(r for r in rows if r.ebay_transaction_id == "9000000001")
        row_short = next(r for r in rows if r.ebay_transaction_id == "9000000002")
        mark_row_matched(conn, row_ok.id, sku_ok)
        mark_row_matched(conn, row_short.id, sku_short)

        results = process_confirmed_rows(conn, batch_id=summary.batch_id)
        results_by_txn = {r.ebay_transaction_id: r for r in results}
        assert results_by_txn["9000000001"].success is True
        assert results_by_txn["9000000002"].success is False
        assert results_by_txn["9000000002"].error_type == "InsufficientStockError"

        # The successful row's depletion really landed...
        assert get_item_stats(conn, sku_ok).quantity == 7
        # ...and the failed row cleanly reverted to 'matched' for retry,
        # NOT stuck at some half-posted state, and its error is visible.
        refreshed = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)
        failed_row = next(r for r in refreshed if r.ebay_transaction_id == "9000000002")
        assert failed_row.review_status == "matched"
        assert failed_row.last_process_error is not None
        assert get_item_stats(conn, sku_short).quantity == 1  # untouched

    def test_processing_twice_is_a_no_op_the_second_time(self, conn):
        sku = _buy_fungible(conn, "Air Filter", quantity=10, price_value=50_000)
        summary = import_csv(conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Air Filter", qty=2)]))
        row = list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)[0]
        mark_row_matched(conn, row.id, sku)

        first = process_confirmed_rows(conn, batch_id=summary.batch_id)
        assert len(first) == 1 and first[0].success is True

        second = process_confirmed_rows(conn, batch_id=summary.batch_id)
        assert second == []  # nothing left in 'matched' status

        assert get_item_stats(conn, sku).quantity == 8  # only depleted once

    def test_refund_rows_are_never_touched_by_processing(self, conn):
        summary = import_csv(
            conn,
            "t.csv",
            _csv(
                [
                    _order_row("11-00000-00001", "9000000001", "Widget A"),
                    _refund_row("11-00000-00002", "Widget B"),
                ]
            ),
        )
        results = process_confirmed_rows(conn, batch_id=summary.batch_id)
        assert results == []  # the Order row was never matched, and Refund rows are never eligible

        refund_row = next(
            r for r in list_review_rows(conn, batch_id=summary.batch_id, include_suggestions=False)
            if r.row_type == "Refund"
        )
        assert refund_row.review_status == "pending"


class TestEbayTransactionIdDbLevelBackstop:
    """Belt-and-suspenders layer (see migrations/004's own comment): even
    bypassing the review-queue entirely and calling the depletion engine
    directly, the same real eBay transaction can never post twice.
    """

    def test_duplicate_ebay_transaction_id_rejected_for_fungible_depletion(self, conn):
        from inventory.depletions import deplete_fungible

        sku = _buy_fungible(conn, "Dup Test Widget", quantity=20, price_value=10_000)
        deplete_fungible(conn, sku, quantity=2, ebay_transaction_id="9999999999")
        with pytest.raises(DuplicateEbaySaleError):
            deplete_fungible(conn, sku, quantity=2, ebay_transaction_id="9999999999")
        # The connection is still usable, and only the first depletion posted.
        assert get_item_stats(conn, sku).quantity == 18

    def test_duplicate_ebay_transaction_id_rejected_for_serial_depletion(self, conn):
        from inventory.depletions import deplete_serial_unit

        sku, serials = _buy_serialized(conn, "Dup Test Watch", quantity=2, total_price=2_000_000)
        deplete_serial_unit(conn, serials[0], ebay_transaction_id="9999999999-unit1")
        with pytest.raises(DuplicateEbaySaleError):
            deplete_serial_unit(conn, serials[1], ebay_transaction_id="9999999999-unit1")
        assert get_item_stats(conn, sku).quantity == 1


class TestSerialOptionsHelper:
    def test_returns_only_on_hand_units(self, conn):
        from inventory.depletions import deplete_serial_unit

        sku, serials = _buy_serialized(conn, "ECU Unit", quantity=3, total_price=15_000_000, category_code="AUTOMOTIVE")
        deplete_serial_unit(conn, serials[0])

        options = get_on_hand_serials_for_sku(conn, sku)
        option_ids = {o["serial_id"] for o in options}
        assert serials[0] not in option_ids
        assert serials[1] in option_ids and serials[2] in option_ids

    def test_fungible_item_returns_empty_list(self, conn):
        sku = _buy_fungible(conn, "Oil Filter 2", quantity=5, price_value=50_000)
        assert get_on_hand_serials_for_sku(conn, sku) == []


class TestProcessConfirmedRowsGenuineConcurrency:
    """Races two real threads both trying to process the SAME already-
    'matched' row via two independent connections — exactly the scenario
    CLAUDE.md's idempotency requirement calls out ("a genuine concurrent
    ... import can't slip through"), applied to double-processing rather
    than double-import. Only one may actually deplete; the row must never
    end up double-posted.
    """

    def test_two_connections_racing_to_process_the_same_row(self, engine):
        with engine.begin() as setup_conn:
            from inventory.seed import seed_categories

            seed_categories(setup_conn)
            sku = _buy_fungible(setup_conn, "Race Widget", quantity=10, price_value=100_000)
            summary = import_csv(
                setup_conn, "t.csv", _csv([_order_row("11-00000-00001", "9000000001", "Race Widget", qty=4)])
            )
            row = list_review_rows(setup_conn, batch_id=summary.batch_id, include_suggestions=False)[0]
            mark_row_matched(setup_conn, row.id, sku)

        barrier = threading.Barrier(2, timeout=10)
        results: dict[str, list[ProcessResult]] = {}

        def racer(name: str):
            conn = engine.connect()
            try:
                barrier.wait()
                with conn.begin():
                    results[name] = process_confirmed_rows(conn, batch_id=summary.batch_id)
            finally:
                conn.close()

        ta = threading.Thread(target=racer, args=("a",))
        tb = threading.Thread(target=racer, args=("b",))
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        successes = [r for results_list in results.values() for r in results_list if r.success]
        assert len(successes) == 1, f"Exactly one racer should have posted the depletion, got: {results}"

        with engine.connect() as verify_conn:
            dep_count = verify_conn.execute(text("SELECT COUNT(*) FROM fungible_depletions")).scalar_one()
            assert dep_count == 1, "The row must never be depleted twice under real concurrency."
            qty = verify_conn.execute(
                text(
                    "SELECT COALESCE(SUM(quantity), 0) FROM fungible_depletions fd "
                    "JOIN items i ON i.id = fd.item_id WHERE i.sku = :sku"
                ),
                {"sku": sku},
            ).scalar_one()
            assert qty == 4
