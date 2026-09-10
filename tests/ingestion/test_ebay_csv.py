"""Parses the REAL sample eBay Transaction report CSVs (not synthetic
fixtures alone — see CLAUDE.md's Milestone 6 brief), plus a handful of
synthetic edge cases the real files don't happen to cover on their own.

The real samples live in the sibling Project-Noctrowl repo
(``sample-documents/``), not in this repo — Builder is explicitly told to
read, never write, there. These tests are skipped (not failed) if that
sibling checkout isn't present, so the suite still runs cleanly in an
environment that only has this repo checked out; wherever the sibling repo
IS present (the real dev environment this milestone was built against),
they run for real.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ingestion.ebay_csv import (
    EbayCsvFormatError,
    parse_ebay_transaction_report,
)

_SAMPLE_ROOT = Path("/Users/sukmasilas/Projects/project-noctrowl-2/sample-documents")
_ACCOUNT_1_DIR = _SAMPLE_ROOT / "eBay account 1_ricky-game"
_ACCOUNT_2_DIR = _SAMPLE_ROOT / "eBay account 2_ricky-garage"

_ACCOUNT_1_FILES = sorted(_ACCOUNT_1_DIR.glob("*.csv")) if _ACCOUNT_1_DIR.is_dir() else []
_ACCOUNT_2_FILES = sorted(_ACCOUNT_2_DIR.glob("*.csv")) if _ACCOUNT_2_DIR.is_dir() else []

requires_real_samples = pytest.mark.skipif(
    not _ACCOUNT_1_FILES, reason="Sibling project-noctrowl-2 sample-documents checkout not found."
)


class TestRealSampleFiles:
    @requires_real_samples
    def test_finds_header_past_the_multiline_preamble_and_parses_every_month(self):
        for path in _ACCOUNT_1_FILES:
            text = path.read_text(encoding="utf-8-sig")
            result = parse_ebay_transaction_report(text)
            assert result.seller == "ricky.game"
            # Every real month has real Order line items and at least one
            # other row type (Other fee is present in all 4 samples).
            assert len(result.rows) > 0
            assert any(r.row_type == "Order" for r in result.rows)
            assert "Other fee" in result.type_counts

    @requires_real_samples
    def test_every_order_row_stored_has_real_item_detail_not_a_rollup_row(self):
        text = (_ACCOUNT_1_DIR / "Transaction_report_20260801_20260831.csv").read_text(encoding="utf-8-sig")
        result = parse_ebay_transaction_report(text)
        order_rows = [r for r in result.rows if r.row_type == "Order"]
        assert len(order_rows) > 0
        for row in order_rows:
            assert row.item_title != "(no item title on this row)"
            assert row.ebay_transaction_id is not None
            assert row.quantity is not None and row.quantity >= 1
        # August 2026 is confirmed (see CLAUDE.md) to contain at least one
        # real multi-item order (17-15092-65817) whose rollup row has no
        # item detail — it must be counted, not silently dropped from the
        # summary, and not present among the stored Order rows.
        assert result.order_summary_rows_skipped >= 1
        assert not any(r.order_number == "17-15092-65817" and r.item_id is None for r in order_rows)

    @requires_real_samples
    def test_transaction_id_is_unique_among_stored_order_rows_across_all_real_files(self):
        seen: set[str] = set()
        for path in _ACCOUNT_1_FILES + _ACCOUNT_2_FILES:
            text = path.read_text(encoding="utf-8-sig")
            result = parse_ebay_transaction_report(text)
            for row in result.rows:
                if row.row_type != "Order":
                    continue
                assert row.ebay_transaction_id not in seen, (
                    f"Transaction ID {row.ebay_transaction_id!r} appeared twice among real Order "
                    f"rows — this is the exact invariant migration 004's unique index relies on."
                )
                seen.add(row.ebay_transaction_id)
        assert len(seen) > 300  # sanity: real data, not an empty/trivial run

    @requires_real_samples
    def test_a_real_cross_month_hold_row_legitimately_reuses_an_order_rows_transaction_id(self):
        """Confirmed real data (CLAUDE.md's Milestone 6 note): Transaction
        ID 10081929607901 is a real Order row in the June 2026 file, and
        ALSO appears on a Hold-placed/Hold-released pair in the July 2026
        file for the same underlying order under review. Since Hold rows
        are never stored (only Order/Refund are — see module docstring),
        this must not be treated as a duplicate Order row and must not
        raise or get skipped as one.
        """
        june_text = (_ACCOUNT_1_DIR / "Transaction_report_20260601_20260630.csv").read_text(encoding="utf-8-sig")
        july_text = (_ACCOUNT_1_DIR / "Transaction_report_20260701_20260731.csv").read_text(encoding="utf-8-sig")
        june = parse_ebay_transaction_report(june_text)
        july = parse_ebay_transaction_report(july_text)

        june_order = [r for r in june.rows if r.ebay_transaction_id == "10081929607901"]
        assert len(june_order) == 1
        assert june_order[0].row_type == "Order"

        july_order = [r for r in july.rows if r.ebay_transaction_id == "10081929607901"]
        assert july_order == []  # the Hold rows are never stored
        assert july.type_counts.get("Hold", 0) >= 2

    @requires_real_samples
    def test_account_2_custom_label_is_populated_but_not_treated_as_sku(self):
        """Real finding (CLAUDE.md): Account 2's Custom label carries a
        sourcing/vendor reference, unrelated to Alakazam item identity —
        this module must still just pass it through verbatim as a display
        field, never interpret or validate it as a SKU.
        """
        text = _ACCOUNT_2_FILES[0].read_text(encoding="utf-8-sig")
        result = parse_ebay_transaction_report(text)
        order_rows = [r for r in result.rows if r.row_type == "Order"]
        labeled = [r for r in order_rows if r.custom_label]
        assert len(labeled) > 0
        assert any("SCRAPED" in (r.custom_label or "") or "EBAY-SOURCE" in (r.custom_label or "") for r in labeled)

    @requires_real_samples
    def test_refund_rows_have_no_quantity_and_no_transaction_id(self):
        text = (_ACCOUNT_1_DIR / "Transaction_report_20260501_20260531.csv").read_text(encoding="utf-8-sig")
        result = parse_ebay_transaction_report(text)
        refund_rows = [r for r in result.rows if r.row_type == "Refund"]
        assert len(refund_rows) > 0
        for row in refund_rows:
            assert row.quantity is None
            assert row.ebay_transaction_id is None
            assert row.item_title  # still has a real title, just no per-line identifiers


class TestSyntheticEdgeCases:
    def _wrap(self, header_and_rows: str, preamble_lines: int = 9) -> str:
        preamble = "\n".join([f"junk preamble line {i}" for i in range(preamble_lines)])
        return preamble + "\nSeller,test.seller\n" + header_and_rows

    def test_missing_header_raises_format_error(self):
        with pytest.raises(EbayCsvFormatError):
            parse_ebay_transaction_report("just,some,random,csv\n1,2,3,4\n")

    def test_header_not_on_first_line_is_still_found(self):
        header = (
            "Transaction creation date,Type,Order number,Legacy order ID,Buyer username,Buyer name,"
            "Ship to city,Ship to province/region/state,Ship to zip,Ship to country,Net amount,"
            "Payout currency,Payout date,Payout ID,Payout method,Payout status,Reason for hold,"
            "Item ID,Transaction ID,Item title,Custom label,Quantity,Item subtotal,Shipping and handling,"
            "Seller collected tax,eBay collected tax,Final Value Fee - fixed,Final Value Fee - variable,"
            "Regulatory operating fee,\"Very high \"\"item not as described\"\" fee\","
            "Below standard performance fee,International fee,Charity donation,Deposit processing fee,"
            "Gross transaction amount,Transaction currency,Exchange rate,Reference ID,Description\n"
            '"Jan 5, 2026",Order,11-00000-00001,11-00000-00001,buyer1,Buyer One,City,ST,00000,US,'
            "10,USD,--,--,--,--,--,1000000001,9000000001,A Test Item,--,1,10,0,--,--,-0.4,-1,--,--,--,-0.1,--,--,10,USD,--,--,--\n"
        )
        result = parse_ebay_transaction_report(self._wrap(header))
        assert result.seller == "test.seller"
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.row_type == "Order"
        assert row.transaction_date == date(2026, 1, 5)
        assert row.ebay_transaction_id == "9000000001"
        assert row.item_title == "A Test Item"
        assert row.custom_label is None
        assert row.quantity == 1

    def _minimal_csv(self, data_rows: list[str]) -> str:
        header = (
            "Transaction creation date,Type,Order number,Legacy order ID,Buyer username,Buyer name,"
            "Ship to city,Ship to province/region/state,Ship to zip,Ship to country,Net amount,"
            "Payout currency,Payout date,Payout ID,Payout method,Payout status,Reason for hold,"
            "Item ID,Transaction ID,Item title,Custom label,Quantity,Item subtotal,Shipping and handling,"
            "Seller collected tax,eBay collected tax,Final Value Fee - fixed,Final Value Fee - variable,"
            "Regulatory operating fee,Very high fee,Below standard performance fee,International fee,"
            "Charity donation,Deposit processing fee,Gross transaction amount,Transaction currency,"
            "Exchange rate,Reference ID,Description\n"
        )
        return self._wrap(header + "\n".join(data_rows) + "\n")

    def test_order_rollup_row_with_no_item_detail_is_skipped_and_counted(self):
        rollup = (
            '"Aug 31, 2026",Order,20-00000-00001,20-00000-00001,b,B,City,ST,0,US,19.4,USD,'
            "--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,23.26,USD,--,--,--"
        )
        detail = (
            '"Aug 31, 2026",Order,20-00000-00001,20-00000-00001,b,B,City,ST,0,US,--,--,'
            "--,--,--,--,--,900000001,8000000001,Real Item One,--,1,5,11,--,--,--,-2.2,--,--,--,-0.16,--,--,--,--,--,--,--"
        )
        result = parse_ebay_transaction_report(self._minimal_csv([rollup, detail]))
        assert result.order_summary_rows_skipped == 1
        assert len(result.rows) == 1
        assert result.rows[0].item_title == "Real Item One"

    def test_hold_other_fee_and_payout_rows_are_not_stored_but_are_counted(self):
        other_fee = (
            '"Aug 31, 2026",Other fee,17-00000-00001,17-00000-00001,b,B,--,--,--,--,-0.89,USD,'
            "--,--,--,--,--,158000000001,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,-0.89,USD,--,FEE-1,desc"
        )
        payout = (
            '"Jun 30, 2026",Payout,--,--,--,--,--,--,--,--,"-3,666.78",USD,--,7593167856,'
            "PAYONEER,Funds sent,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,--,\"-3,666.78\",USD,--,--,Scheduled payout"
        )
        hold_placed = (
            '"Jul 30, 2026",Hold,01-00000-00001,01-00000-00001,b,B,City,ST,0,US,-152,USD,'
            "--,--,--,--,--,157000000001,9000000002,Held Item,--,1,162.26,--,--,--,--,--,--,--,--,--,--,--,-152,USD,--,Case ID,Hold placed"
        )
        result = parse_ebay_transaction_report(self._minimal_csv([other_fee, payout, hold_placed]))
        assert result.rows == []
        assert result.type_counts == {"Other fee": 1, "Payout": 1, "Hold": 1}

    def test_refund_row_is_stored_with_no_quantity_or_transaction_id(self):
        refund = (
            '"May 31, 2026",Refund,11-00000-00001,11-00000-00001,b,B,City,ST,0,US,-85.57,USD,'
            "--,--,--,--,--,--,--,Refunded Item,--,--,--,--,--,-7.14,0.44,14.96,--,--,--,1.03,--,--,-102,USD,--,Cancel ID,--"
        )
        result = parse_ebay_transaction_report(self._minimal_csv([refund]))
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.row_type == "Refund"
        assert row.quantity is None
        assert row.ebay_transaction_id is None
        assert row.item_title == "Refunded Item"

    def test_order_row_with_item_detail_but_missing_transaction_id_is_still_parsed(self):
        """The parser itself always stores what it finds — refusing to
        import a row with no Transaction ID (for idempotency reasons) is
        ingestion/ebay_import.py's job, not this module's (see
        test_ebay_import.py). This module just reports what's really
        there, verbatim.
        """
        weird = (
            '"Aug 31, 2026",Order,30-00000-00001,30-00000-00001,b,B,City,ST,0,US,--,--,'
            "--,--,--,--,--,900000002,--,Weird Item No Txn Id,--,1,5,0,--,--,--,-1,--,--,--,-0.1,--,--,--,--,--,--,--"
        )
        result = parse_ebay_transaction_report(self._minimal_csv([weird]))
        assert len(result.rows) == 1
        assert result.rows[0].ebay_transaction_id is None
