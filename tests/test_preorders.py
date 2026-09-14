"""Tests for inventory/preorders.py — Milestone 7 pre-order/dropship sales.

Runs against a real Postgres database (same reasoning as every other
integration test in this project — the fungible-only check and the
pending->fulfilled/cancelled compare-and-swap invariants are real database
mechanisms, migrations/005_add_preorder_sales.sql, not just app-level
Python checks).
"""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inventory.exceptions import (
    InsufficientStockError,
    PreorderItemMismatchError,
    PreorderSaleNotFoundError,
    PreorderSaleNotPendingError,
    ValidationError,
)
from inventory.items import create_item
from inventory.preorders import (
    cancel_preorder_sale,
    fulfill_preorder_sales,
    get_preorder_sale,
    list_preorder_sales,
    record_preorder_sale,
)
from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase
from inventory.queries import get_item_stats


def _new_fungible_item(conn, name="Preorder Widget", category_code="OTHERS"):
    """A brand-new fungible item with ZERO on-hand stock and no purchase at
    all — the real starting condition for a pre-order sale, which by
    definition is recorded BEFORE any purchase exists for the item. Same
    zero-quantity path Milestone 3's "+ Add New Item" panel uses
    (inventory/items.py::create_item).
    """
    item = create_item(conn, name=name, category_code=category_code, identity_mode="fungible")
    return item.sku


def _new_serialized_item(conn, name="Preorder Watch", category_code="WATCHES"):
    item = create_item(conn, name=name, category_code=category_code, identity_mode="serialized")
    return item.sku


def _buy_stock(conn, sku, quantity, price_value, purchase_date=date(2026, 9, 1), vendor="Stock purchase"):
    return save_purchase(
        conn,
        PurchaseInput(
            purchase_date=purchase_date,
            vendor_description=vendor,
            total_amount_paid=Decimal(price_value) * quantity,
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=quantity, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal(price_value),
                )
            ],
        ),
    )


class TestRecordPreorderSale:
    def test_records_pending_sale_for_fungible_item(self, conn):
        sku = _new_fungible_item(conn)
        result = record_preorder_sale(conn, sku, quantity=3, sale_date=date(2026, 9, 14), reference="eBay #1")
        assert result.status == "pending"
        assert result.quantity == 3
        assert result.sku == sku
        assert result.reference == "eBay #1"
        assert result.sale_date == date(2026, 9, 14)

    def test_no_sale_price_or_revenue_field_anywhere(self, conn):
        """Same standing rule as every prior milestone — confirmed by
        introspection, not just by not writing one.
        """
        sku = _new_fungible_item(conn)
        result = record_preorder_sale(conn, sku, quantity=1)
        fields = vars(result).keys()
        assert not any("price" in f.lower() or "revenue" in f.lower() for f in fields)

    def test_rejects_serialized_item_with_clear_error(self, conn):
        sku = _new_serialized_item(conn)
        with pytest.raises(ValidationError, match="fungible"):
            record_preorder_sale(conn, sku, quantity=1)

    def test_rejects_nonexistent_sku(self, conn):
        with pytest.raises(ValidationError):
            record_preorder_sale(conn, "NOPE-0001", quantity=1)

    def test_rejects_nonpositive_quantity(self, conn):
        sku = _new_fungible_item(conn)
        with pytest.raises(ValidationError):
            record_preorder_sale(conn, sku, quantity=0)
        with pytest.raises(ValidationError):
            record_preorder_sale(conn, sku, quantity=-1)

    def test_default_sale_date_is_today(self, conn):
        sku = _new_fungible_item(conn)
        result = record_preorder_sale(conn, sku, quantity=1)
        assert result.sale_date == date.today()

    def test_db_level_fungible_check_is_a_real_backstop(self, conn):
        """Bypasses the app-level check entirely with a raw INSERT — the
        real DB trigger (migrations/005_add_preorder_sales.sql) must still
        reject a serialized item, same two-layer discipline this project
        already applies to every other cross-table invariant.
        """
        sku = _new_serialized_item(conn)
        item_id = conn.execute(text("SELECT id FROM items WHERE sku = :sku"), {"sku": sku}).scalar_one()
        # pytest.raises MUST wrap conn.begin_nested() itself (not the other
        # way around) so the savepoint's own __exit__ sees the exception
        # and issues ROLLBACK TO SAVEPOINT — the same reasoning
        # inventory/purchases.py's own docstring gives for why every real
        # savepoint-guarded insert in this codebase is structured this way.
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO preorder_sales (item_id, quantity, sale_date) "
                        "VALUES (:item_id, 1, :sale_date)"
                    ),
                    {"item_id": item_id, "sale_date": date(2026, 9, 14)},
                )


class TestListAndGetPreorderSale:
    def test_list_filters_by_status_and_sku(self, conn):
        sku_a = _new_fungible_item(conn, name="Item A")
        sku_b = _new_fungible_item(conn, name="Item B")
        p1 = record_preorder_sale(conn, sku_a, quantity=1)
        record_preorder_sale(conn, sku_b, quantity=1)
        cancel_preorder_sale(conn, p1.id)

        assert len(list_preorder_sales(conn)) == 2
        assert len(list_preorder_sales(conn, status="pending")) == 1
        assert len(list_preorder_sales(conn, status="cancelled")) == 1
        assert len(list_preorder_sales(conn, sku=sku_a)) == 1

    def test_get_nonexistent_raises(self, conn):
        with pytest.raises(PreorderSaleNotFoundError):
            get_preorder_sale(conn, 999999)


class TestCancelPreorderSale:
    def test_cancels_pending_sale(self, conn):
        sku = _new_fungible_item(conn)
        created = record_preorder_sale(conn, sku, quantity=2)
        cancelled = cancel_preorder_sale(conn, created.id)
        assert cancelled.status == "cancelled"

    def test_cancelling_nonexistent_id_raises_not_found(self, conn):
        with pytest.raises(PreorderSaleNotFoundError):
            cancel_preorder_sale(conn, 999999)

    def test_cancelling_already_cancelled_sale_fails(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        cancel_preorder_sale(conn, preorder.id)
        with pytest.raises(PreorderSaleNotPendingError):
            cancel_preorder_sale(conn, preorder.id)

    def test_cancelling_already_fulfilled_sale_fails(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=2)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Fulfillment purchase",
            total_amount_paid=Decimal("200000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=2, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("100000"),
                )
            ],
        )
        fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)
        with pytest.raises(PreorderSaleNotPendingError):
            cancel_preorder_sale(conn, preorder.id)


class TestFulfillPreorderSalesSingle:
    def test_fulfilling_single_preorder_exact_quantity_match(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=5)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Bulk buy",
            total_amount_paid=Decimal("500000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=5, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("100000"),
                )
            ],
        )
        result = fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)

        assert result.purchase is not None
        assert result.purchase.id == result.purchase_id
        assert len(result.fulfilled) == 1
        depletion = result.fulfilled[0].depletion
        assert depletion.quantity == 5
        assert depletion.unit_cost == Decimal("100000")
        assert depletion.total_cost == 500000
        assert depletion.preorder_sale_id == preorder.id

        assert get_preorder_sale(conn, preorder.id).status == "fulfilled"

        # Purchase quantity exactly matched the pre-order quantity — no
        # extra stock should remain on hand.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 0
        assert stats.cost_basis == Decimal("0")

    def test_fulfilling_leaves_leftover_stock_when_purchase_exceeds_preorder_quantity(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=3)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Bulk buy with surplus",
            total_amount_paid=Decimal("1000000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=10, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("100000"),
                )
            ],
        )
        result = fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)
        depletion = result.fulfilled[0].depletion
        assert depletion.quantity == 3
        assert depletion.unit_cost == Decimal("100000")
        assert depletion.total_cost == 300000

        # 10 purchased, only 3 depleted for the pre-order -> 7 genuinely
        # remain as real on-hand stock.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 7
        assert stats.cost_basis == Decimal("700000")

    def test_weighted_average_cost_includes_preexisting_stock(self, conn):
        """The fulfilling purchase's cost genuinely JOINS the weighted-
        average pool alongside any pre-existing stock — not an isolated/
        FIFO-batch computation (reuses Milestone 5's unmodified engine).
        """
        sku = _new_fungible_item(conn)
        _buy_stock(conn, sku, quantity=5, price_value=50_000, vendor="Existing stock")  # 5 @ 50,000
        preorder = record_preorder_sale(conn, sku, quantity=5)
        purchase = PurchaseInput(  # 5 @ 150,000
            purchase_date=date(2026, 9, 14),
            vendor_description="Fulfilling purchase",
            total_amount_paid=Decimal("750000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=5, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("150000"),
                )
            ],
        )
        # Weighted average across 10 units: (250,000 + 750,000) / 10 = 100,000
        result = fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)
        depletion = result.fulfilled[0].depletion
        assert depletion.unit_cost == Decimal("100000")
        assert depletion.total_cost == 500000

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 5
        assert stats.cost_basis == Decimal("500000")

    def test_requires_exactly_one_of_purchase_or_purchase_id(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        with pytest.raises(ValidationError):
            fulfill_preorder_sales(conn, [preorder.id])

        existing = _buy_stock(conn, sku, quantity=1, price_value=100_000)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14), vendor_description="x", total_amount_paid=Decimal("1"),
            shipping_mode="none", lines=[],
        )
        with pytest.raises(ValidationError):
            fulfill_preorder_sales(conn, [preorder.id], purchase=purchase, purchase_id=existing.id)

    def test_rejects_empty_preorder_sale_id_list(self, conn):
        sku = _new_fungible_item(conn)
        purchase = _buy_stock(conn, sku, quantity=1, price_value=100_000)
        with pytest.raises(ValidationError):
            fulfill_preorder_sales(conn, [], purchase_id=purchase.id)

    def test_rejects_already_fulfilled_preorder(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        purchase1 = PurchaseInput(
            purchase_date=date(2026, 9, 14), vendor_description="First fulfillment",
            total_amount_paid=Decimal("100000"), shipping_mode="none",
            lines=[PurchaseLineInput(sku=sku, quantity=1, pricing_mode="direct", price_entry_mode="per_unit", price_value=Decimal("100000"))],
        )
        fulfill_preorder_sales(conn, [preorder.id], purchase=purchase1)

        purchase2 = PurchaseInput(
            purchase_date=date(2026, 9, 15), vendor_description="Second attempt",
            total_amount_paid=Decimal("100000"), shipping_mode="none",
            lines=[PurchaseLineInput(sku=sku, quantity=1, pricing_mode="direct", price_entry_mode="per_unit", price_value=Decimal("100000"))],
        )
        with pytest.raises(PreorderSaleNotPendingError):
            fulfill_preorder_sales(conn, [preorder.id], purchase=purchase2)

    def test_rejects_already_cancelled_preorder(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        cancel_preorder_sale(conn, preorder.id)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14), vendor_description="Attempt after cancel",
            total_amount_paid=Decimal("100000"), shipping_mode="none",
            lines=[PurchaseLineInput(sku=sku, quantity=1, pricing_mode="direct", price_entry_mode="per_unit", price_value=Decimal("100000"))],
        )
        with pytest.raises(PreorderSaleNotPendingError):
            fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)

    def test_rejects_nonexistent_purchase_id(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        with pytest.raises(ValidationError):
            fulfill_preorder_sales(conn, [preorder.id], purchase_id=999999)

    def test_rejects_nonexistent_preorder_sale_id(self, conn):
        sku = _new_fungible_item(conn)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14), vendor_description="x",
            total_amount_paid=Decimal("100000"), shipping_mode="none",
            lines=[PurchaseLineInput(sku=sku, quantity=1, pricing_mode="direct", price_entry_mode="per_unit", price_value=Decimal("100000"))],
        )
        with pytest.raises(PreorderSaleNotFoundError):
            fulfill_preorder_sales(conn, [999999], purchase=purchase)

    def test_failed_fulfillment_rolls_back_the_newly_created_purchase(self, engine):
        """If the given purchase doesn't actually bring enough stock to
        cover the requested pre-order sale, the WHOLE fulfillment
        (including the brand-new purchase and the pending->fulfilled
        transition) must roll back — never leave a half-posted purchase
        with no matching depletion, and never leave the pre-order sale
        stuck 'fulfilled' with nothing actually deducted. Uses a real,
        separate connection (not the shared ``conn`` fixture, which would
        also roll back the pre-order's own setup) — mirrors exactly how
        the real caller (webapp/dbdep.py::get_write_conn's engine.begin())
        rolls back the whole request transaction on an uncaught exception.
        """
        from inventory.seed import seed_categories

        with engine.begin() as setup_conn:
            seed_categories(setup_conn)
            sku = _new_fungible_item(setup_conn)
            preorder = record_preorder_sale(setup_conn, sku, quantity=10)
        preorder_id = preorder.id

        conn = engine.connect()
        try:
            purchase = PurchaseInput(
                purchase_date=date(2026, 9, 14),
                vendor_description="Insufficient purchase for the pre-order",
                total_amount_paid=Decimal("300000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        sku=sku, quantity=3, pricing_mode="direct",
                        price_entry_mode="per_unit", price_value=Decimal("100000"),
                    )
                ],
            )
            with pytest.raises(InsufficientStockError):
                fulfill_preorder_sales(conn, [preorder_id], purchase=purchase)
        finally:
            conn.rollback()
            conn.close()

        with engine.connect() as verify_conn:
            assert get_preorder_sale(verify_conn, preorder_id).status == "pending"
            stats = get_item_stats(verify_conn, sku)
            assert stats.quantity == 0
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM purchases "
                    "WHERE vendor_description = 'Insufficient purchase for the pre-order'"
                )
            ).scalar_one()
            assert count == 0


class TestFulfillPreorderSalesMultiple:
    def test_fulfilling_multiple_preorders_from_one_bulk_purchase(self, conn):
        sku = _new_fungible_item(conn)
        p1 = record_preorder_sale(conn, sku, quantity=2)
        p2 = record_preorder_sale(conn, sku, quantity=3)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Bulk buy covering two pre-orders",
            total_amount_paid=Decimal("500000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku, quantity=5, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("100000"),
                )
            ],
        )
        result = fulfill_preorder_sales(conn, [p1.id, p2.id], purchase=purchase)
        assert len(result.fulfilled) == 2
        assert {f.preorder_sale_id for f in result.fulfilled} == {p1.id, p2.id}
        assert get_preorder_sale(conn, p1.id).status == "fulfilled"
        assert get_preorder_sale(conn, p2.id).status == "fulfilled"

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 0
        assert stats.cost_basis == Decimal("0")

    def test_fulfilling_via_existing_purchase_id(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=4)
        existing = _buy_stock(conn, sku, quantity=4, price_value=100_000, vendor="Already-saved purchase")

        result = fulfill_preorder_sales(conn, [preorder.id], purchase_id=existing.id)
        assert result.purchase is None
        assert result.purchase_id == existing.id
        assert get_preorder_sale(conn, preorder.id).status == "fulfilled"

    def test_rejects_mismatched_items_in_one_batch(self, conn):
        sku_a = _new_fungible_item(conn, name="Item A")
        sku_b = _new_fungible_item(conn, name="Item B")
        p_a = record_preorder_sale(conn, sku_a, quantity=1)
        p_b = record_preorder_sale(conn, sku_b, quantity=1)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Mismatched fulfillment attempt",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=sku_a, quantity=1, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("100000"),
                )
            ],
        )
        with pytest.raises(PreorderItemMismatchError):
            fulfill_preorder_sales(conn, [p_a.id, p_b.id], purchase=purchase)

        # Neither pre-order sale should have been touched.
        assert get_preorder_sale(conn, p_a.id).status == "pending"
        assert get_preorder_sale(conn, p_b.id).status == "pending"

    def test_rejects_purchase_with_no_line_for_the_preorders_item(self, conn):
        sku = _new_fungible_item(conn, name="Wanted Item")
        other_sku = _new_fungible_item(conn, name="Unrelated Item")
        preorder = record_preorder_sale(conn, sku, quantity=1)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14),
            vendor_description="Wrong item purchase",
            total_amount_paid=Decimal("50000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    sku=other_sku, quantity=1, pricing_mode="direct",
                    price_entry_mode="per_unit", price_value=Decimal("50000"),
                )
            ],
        )
        with pytest.raises(PreorderItemMismatchError):
            fulfill_preorder_sales(conn, [preorder.id], purchase=purchase)
        assert get_preorder_sale(conn, preorder.id).status == "pending"

    def test_rejects_duplicate_ids_in_one_request(self, conn):
        sku = _new_fungible_item(conn)
        preorder = record_preorder_sale(conn, sku, quantity=1)
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 14), vendor_description="x",
            total_amount_paid=Decimal("100000"), shipping_mode="none",
            lines=[PurchaseLineInput(sku=sku, quantity=1, pricing_mode="direct", price_entry_mode="per_unit", price_value=Decimal("100000"))],
        )
        with pytest.raises(ValidationError):
            fulfill_preorder_sales(conn, [preorder.id, preorder.id], purchase=purchase)


# --------------------------------------------------------------------- #
# Genuine (barrier-synchronized) concurrency — the ONE genuinely new
# invariant this milestone needs: the pending->fulfilled/cancelled
# compare-and-swap. Uses threading.Barrier (true simultaneity), NOT
# event-ordered staggering — exactly the distinction that let Milestone 5's
# original trigger-based design pass its first-pass tests while still
# harboring a real deadlock (see migrations/003_add_depletions.sql /
# tests/test_depletions.py for the full history this project learned from).
# --------------------------------------------------------------------- #


def _fresh_pending_preorder_with_stock(engine, quantity, stock_quantity, price_value=100_000):
    with engine.begin() as setup_conn:
        from inventory.seed import seed_categories

        seed_categories(setup_conn)
        sku = _new_fungible_item(setup_conn, name="Barrier Preorder Widget")
        _buy_stock(setup_conn, sku, quantity=stock_quantity, price_value=price_value, vendor="Pre-existing race stock")
        preorder = record_preorder_sale(setup_conn, sku, quantity=quantity)
    return sku, preorder.id


class TestFulfillPreorderSalesGenuineConcurrency:
    TRIALS = 15

    def test_two_simultaneous_fulfillment_attempts_only_one_wins(self, engine):
        """Two threads race to fulfill the SAME pending pre-order sale,
        each referencing its OWN already-saved purchase (by purchase_id) —
        isolates this test to the pending->fulfilled compare-and-swap
        invariant alone, not save_purchase()'s own concurrency behavior
        (already covered independently by Milestone 2's test suite).
        Exactly one thread should succeed; the other must get a clean
        PreorderSaleNotPendingError, never a deadlock or crash.
        """
        unexpected = []
        for _ in range(self.TRIALS):
            sku, preorder_id = _fresh_pending_preorder_with_stock(engine, quantity=5, stock_quantity=20)
            with engine.begin() as setup_conn:
                purchase = save_purchase(
                    setup_conn,
                    PurchaseInput(
                        purchase_date=date(2026, 9, 14),
                        vendor_description="Fulfilling purchase for the race",
                        total_amount_paid=Decimal("500000"),
                        shipping_mode="none",
                        lines=[
                            PurchaseLineInput(
                                sku=sku, quantity=5, pricing_mode="direct",
                                price_entry_mode="per_unit", price_value=Decimal("100000"),
                            )
                        ],
                    ),
                )

            barrier = threading.Barrier(2, timeout=10)
            results: dict = {}

            def racer(name: str):
                conn = engine.connect()
                try:
                    barrier.wait()
                    try:
                        fulfill_preorder_sales(conn, [preorder_id], purchase_id=purchase.id)
                        conn.commit()
                        results[name] = "success"
                    except PreorderSaleNotPendingError:
                        conn.rollback()
                        results[name] = "rejected"
                    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
                        conn.rollback()
                        results[name] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
                finally:
                    conn.close()

            ta = threading.Thread(target=racer, args=("a",))
            tb = threading.Thread(target=racer, args=("b",))
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            outcomes = sorted(results.values())
            if any(v.startswith("UNEXPECTED") for v in outcomes):
                unexpected.append(outcomes)
                continue
            assert outcomes == ["rejected", "success"], outcomes

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials produced an unexpected outcome "
            f"(deadlock or other uncaught error) instead of a clean success/rejection pair: {unexpected}"
        )

    def test_fulfill_vs_cancel_race_exactly_one_wins(self, engine):
        """One thread tries to fulfill, another tries to cancel, the SAME
        pending pre-order sale, at the exact same instant. Exactly one must
        win; the other must get a clean PreorderSaleNotPendingError.
        """
        unexpected = []
        for _ in range(self.TRIALS):
            sku, preorder_id = _fresh_pending_preorder_with_stock(engine, quantity=3, stock_quantity=10)
            with engine.begin() as setup_conn:
                purchase = save_purchase(
                    setup_conn,
                    PurchaseInput(
                        purchase_date=date(2026, 9, 14),
                        vendor_description="Fulfilling purchase for the fulfill-vs-cancel race",
                        total_amount_paid=Decimal("300000"),
                        shipping_mode="none",
                        lines=[
                            PurchaseLineInput(
                                sku=sku, quantity=3, pricing_mode="direct",
                                price_entry_mode="per_unit", price_value=Decimal("100000"),
                            )
                        ],
                    ),
                )

            barrier = threading.Barrier(2, timeout=10)
            results: dict = {}

            def fulfiller():
                conn = engine.connect()
                try:
                    barrier.wait()
                    try:
                        fulfill_preorder_sales(conn, [preorder_id], purchase_id=purchase.id)
                        conn.commit()
                        results["fulfill"] = "success"
                    except PreorderSaleNotPendingError:
                        conn.rollback()
                        results["fulfill"] = "rejected"
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        results["fulfill"] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
                finally:
                    conn.close()

            def canceller():
                conn = engine.connect()
                try:
                    barrier.wait()
                    try:
                        cancel_preorder_sale(conn, preorder_id)
                        conn.commit()
                        results["cancel"] = "success"
                    except PreorderSaleNotPendingError:
                        conn.rollback()
                        results["cancel"] = "rejected"
                    except Exception as exc:  # noqa: BLE001
                        conn.rollback()
                        results["cancel"] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
                finally:
                    conn.close()

            ta = threading.Thread(target=fulfiller)
            tb = threading.Thread(target=canceller)
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            outcomes = sorted(results.values())
            if any(v.startswith("UNEXPECTED") for v in outcomes):
                unexpected.append(results.copy())
                continue
            assert outcomes == ["rejected", "success"], results

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials produced an unexpected outcome "
            f"(deadlock, double-success, or other uncaught error) instead of a clean "
            f"success/rejection pair: {unexpected}"
        )

    def test_two_different_items_fulfilled_at_the_exact_same_instant_both_succeed(self, engine):
        """QA's own original repro for the purchase_ref race (2026-09-14):
        two threads each fulfilling a DIFFERENT pending pre-order sale, for
        a DIFFERENT item, each via a brand-new Purchase created inline
        (``purchase=...``, not ``purchase_id=...``) — i.e. two genuinely
        independent ``save_purchase()`` calls at the exact same instant,
        which is exactly the real-world "two staff fulfilling different
        pre-orders around the same time" scenario this milestone's
        fulfillment flow makes newly realistic. Before the fix in
        ``inventory/purchases.py::save_purchase()`` (a bounded retry loop
        around ``_generate_purchase_ref()`` + the header INSERT), one side
        got an uncaught ``psycopg2.errors.UniqueViolation`` on
        ``purchases_purchase_ref_key`` — see
        ``tests/test_purchases.py::TestPurchaseRefGenerationGenuineConcurrency``
        for the isolated, pre-order-independent repro of the same
        underlying bug.
        """
        unexpected = []
        for trial in range(self.TRIALS):
            with engine.begin() as setup_conn:
                from inventory.seed import seed_categories

                seed_categories(setup_conn)
                # Deliberately single-word, mutually-distinct names (not
                # sharing a first-two-words SKU slug — see
                # inventory/sku.py::slugify_name) so this test isolates
                # ONLY the purchase_ref race, never a SKU-generation race
                # (a real, separate, already-covered invariant).
                sku_a = _new_fungible_item(setup_conn, name=f"RefRacePreorderItemA{trial}")
                sku_b = _new_fungible_item(setup_conn, name=f"RefRacePreorderItemB{trial}")
                preorder_a = record_preorder_sale(setup_conn, sku_a, quantity=2)
                preorder_b = record_preorder_sale(setup_conn, sku_b, quantity=3)

            barrier = threading.Barrier(2, timeout=10)
            results: dict = {}

            def make_purchase(sku: str, quantity: int, price: int) -> PurchaseInput:
                return PurchaseInput(
                    purchase_date=date(2026, 9, 14),
                    vendor_description=f"Fulfilling purchase for {sku}",
                    total_amount_paid=Decimal(price) * quantity,
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            sku=sku, quantity=quantity, pricing_mode="direct",
                            price_entry_mode="per_unit", price_value=Decimal(price),
                        )
                    ],
                )

            def racer(name: str, preorder_id: int, sku: str, quantity: int):
                conn = engine.connect()
                try:
                    barrier.wait()
                    try:
                        fulfill_preorder_sales(
                            conn, [preorder_id], purchase=make_purchase(sku, quantity, 100_000)
                        )
                        conn.commit()
                        results[name] = "success"
                    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
                        conn.rollback()
                        results[name] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
                finally:
                    conn.close()

            ta = threading.Thread(target=racer, args=("a", preorder_a.id, sku_a, 2))
            tb = threading.Thread(target=racer, args=("b", preorder_b.id, sku_b, 3))
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            if any(v.startswith("UNEXPECTED") for v in results.values()):
                unexpected.append(results.copy())
                continue
            assert results == {"a": "success", "b": "success"}, results

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials produced an unexpected outcome "
            f"(an uncaught exception, e.g. the purchase_ref race) instead of both "
            f"independent fulfillments succeeding cleanly: {unexpected}"
        )
