"""Integration tests for inventory/depletions.py against a real Postgres
database — the negative-stock invariant is enforced partly by real DB
mechanisms (migrations/003_add_depletions.sql's trigger, and the atomic
compare-and-swap UPDATE for serialized units), so these need the real
engine, not a mock. Mirrors tests/test_purchases.py's own structure and
seriousness for its own invariant.
"""
from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from export.csv_builder import build_export_rows
from inventory.depletions import deplete_fungible, deplete_serial_unit
from inventory.exceptions import (
    AlreadyDepletedError,
    DepletionConflictError,
    InsufficientStockError,
    ItemMismatchError,
    SerialUnitNotFoundError,
    ValidationError,
)
from inventory.purchases import (
    NewItemInput,
    PurchaseInput,
    PurchaseLineInput,
    save_purchase,
)
from inventory.queries import get_item_by_sku, get_item_stats, get_purchase_detail


def _buy_fungible(conn, name, quantity, price_value, category_code="AUTOMOTIVE", price_entry_mode="per_unit"):
    result = save_purchase(
        conn,
        PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description=f"Stock-up: {name}",
            total_amount_paid=Decimal(price_value) * quantity if price_entry_mode == "per_unit" else Decimal(price_value),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=NewItemInput(name=name, category_code=category_code, identity_mode="fungible"),
                    quantity=quantity,
                    pricing_mode="direct",
                    price_entry_mode=price_entry_mode,
                    price_value=Decimal(price_value),
                )
            ],
        ),
    )
    return result.lines[0].sku


def _buy_serialized(conn, name, quantity, total_price, category_code="WATCHES", serial_units=None):
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
                    serial_units=serial_units,
                )
            ],
        ),
    )
    return result.lines[0].sku, result.lines[0].serial_ids


class TestDepleteFungibleNormalPath:
    def test_normal_partial_depletion_reduces_on_hand_and_computes_average_cost(self, conn):
        sku = _buy_fungible(conn, "Brake Pad Set", quantity=10, price_value=100_000)
        # on hand: 10 units @ Rp 100,000 = Rp 1,000,000

        result = deplete_fungible(conn, sku, quantity=4, reference="eBay order #12345")

        assert result.quantity == 4
        assert result.unit_cost == Decimal("100000")
        assert result.total_cost == 400_000
        assert result.reference == "eBay order #12345"

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 6
        assert stats.cost_basis == Decimal("600000")

    def test_weighted_average_across_two_purchases_at_different_prices(self, conn):
        # First purchase: 10 @ 100,000 = 1,000,000. Second: 10 @ 200,000 =
        # 2,000,000. Combined: 20 units, 3,000,000 cost basis -> average
        # 150,000/unit.
        item = NewItemInput(name="Mixed Price Widget", category_code="OTHERS", identity_mode="fungible")
        first = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Batch 1",
                total_amount_paid=Decimal("1000000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=item, quantity=10, pricing_mode="direct",
                        price_entry_mode="per_unit", price_value=Decimal("100000"),
                    )
                ],
            ),
        )
        sku = first.lines[0].sku
        save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 15),
                vendor_description="Batch 2, same SKU, higher price",
                total_amount_paid=Decimal("2000000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        sku=sku, quantity=10, pricing_mode="direct",
                        price_entry_mode="per_unit", price_value=Decimal("200000"),
                    )
                ],
            ),
        )

        result = deplete_fungible(conn, sku, quantity=5)
        assert result.unit_cost == Decimal("150000")
        assert result.total_cost == 750_000

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 15
        assert stats.cost_basis == Decimal("2250000")

    def test_exact_full_depletion_to_zero(self, conn):
        sku = _buy_fungible(conn, "Oil Filter", quantity=7, price_value=50_000)
        result = deplete_fungible(conn, sku, quantity=7)
        assert result.total_cost == 350_000

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 0
        assert stats.cost_basis == Decimal("0")

    def test_average_cost_is_unchanged_by_depletion_itself(self, conn):
        """A documented property of moving-weighted-average cost (see the
        design doc): depleting at the current average doesn't itself move
        the average for what's left, since it removes quantity and cost
        basis in the same proportion.
        """
        sku = _buy_fungible(conn, "Spark Plug", quantity=20, price_value=25_000)
        deplete_fungible(conn, sku, quantity=8)
        second = deplete_fungible(conn, sku, quantity=5)
        assert second.unit_cost == Decimal("25000")


class TestDepleteFungibleRejectsOverDepletion:
    def test_python_level_pre_check_rejects_over_depletion(self, conn):
        sku = _buy_fungible(conn, "Turbocharger Kit", quantity=3, price_value=500_000)
        with pytest.raises(InsufficientStockError) as excinfo:
            deplete_fungible(conn, sku, quantity=4)
        assert excinfo.value.available == 3
        assert excinfo.value.requested == 4

        # Nothing was written.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 3

    def test_zero_or_negative_quantity_rejected(self, conn):
        sku = _buy_fungible(conn, "Widget", quantity=5, price_value=1000)
        with pytest.raises(ValidationError):
            deplete_fungible(conn, sku, quantity=0)
        with pytest.raises(ValidationError):
            deplete_fungible(conn, sku, quantity=-1)

    def test_depleting_an_unknown_sku_rejected(self, conn):
        with pytest.raises(ValidationError):
            deplete_fungible(conn, "AUTO-NOPE-9999", quantity=1)

    def test_depleting_a_serialized_item_via_fungible_path_rejected(self, conn):
        sku, _ = _buy_serialized(conn, "Serialized Widget", quantity=1, total_price=100_000)
        with pytest.raises(ValidationError):
            deplete_fungible(conn, sku, quantity=1)


class TestDepleteFungibleDbLevelBackstop:
    def test_raw_sql_bypass_is_rejected_by_the_real_trigger(self, conn):
        """Bypasses deplete_fungible() entirely — proves the DB trigger
        itself is a real, independent backstop, not just relied upon via
        the Python-side pre-check.
        """
        sku = _buy_fungible(conn, "Trigger Test Widget", quantity=2, price_value=100_000)
        item = get_item_by_sku(conn, sku)

        with pytest.raises(IntegrityError, match="would go negative"):
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO fungible_depletions
                            (item_id, quantity, unit_cost, total_cost, depletion_date)
                        VALUES (:item_id, 3, 100000, 300000, '2026-06-02')
                        """
                    ),
                    {"item_id": item.id},
                )

        # Connection is still usable afterward (the savepoint absorbed the abort).
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 2

    def test_raw_sql_insert_against_a_serialized_item_is_rejected(self, conn):
        sku, _ = _buy_serialized(conn, "Serialized Trigger Test", quantity=1, total_price=100_000)
        item = get_item_by_sku(conn, sku)
        with pytest.raises(IntegrityError, match="not fungible"):
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO fungible_depletions
                            (item_id, quantity, unit_cost, total_cost, depletion_date)
                        VALUES (:item_id, 1, 100000, 100000, '2026-06-02')
                        """
                    ),
                    {"item_id": item.id},
                )


class TestDepleteFungibleDeadlockTranslation:
    """QA-found bug, fixed 2026-09-10 (defense-in-depth layer): a real
    Postgres deadlock can no longer be forced deterministically through
    this project's normal call pattern — the advisory-lock fix makes it
    structurally unreachable (a single-lock-per-transaction protocol
    cannot deadlock against another instance of itself; see
    migrations/003_add_depletions.sql's trigger comment and
    TestDepleteFungibleGenuineSimultaneousConcurrency below for the real
    concurrency proof). This test instead verifies the DEFENSIVE
    translation logic itself in isolation, by simulating the exact
    SQLAlchemy/psycopg2 exception shape a real deadlock (SQLSTATE
    ``40P01``) would produce — proving that IF one ever reached this
    function despite the structural fix, it would still surface as a
    clean ``DepletionConflictError`` (never a raw, uncaught 500), and that
    the connection remains usable afterward (same savepoint-absorption
    guarantee as the real IntegrityError backstop path).
    """

    def test_simulated_deadlock_sqlstate_is_translated_cleanly(self, conn):
        sku = _buy_fungible(conn, "Deadlock Simulation Widget", quantity=5, price_value=10_000)

        real_execute = conn.execute

        class _FakeDeadlockOrig(Exception):
            pgcode = "40P01"

        def fake_execute(clause, *args, **kwargs):
            if "INSERT INTO fungible_depletions" in str(clause):
                raise OperationalError("INSERT INTO fungible_depletions ...", {}, _FakeDeadlockOrig())
            return real_execute(clause, *args, **kwargs)

        conn.execute = fake_execute
        try:
            with pytest.raises(DepletionConflictError) as excinfo:
                deplete_fungible(conn, sku, quantity=2)
            assert excinfo.value.sku == sku
        finally:
            conn.execute = real_execute

        # The connection is still usable afterward — the begin_nested()
        # savepoint absorbed the simulated abort, same guarantee the real
        # IntegrityError backstop path already relies on.
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        # And nothing was actually written for the "failed" attempt.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 5

    def test_an_unrelated_operational_error_is_not_swallowed(self, conn):
        """Only SQLSTATE 40P01 gets the DepletionConflictError translation
        — any other OperationalError is a real, unexpected failure and
        must keep propagating as itself, not be silently reinterpreted.
        """
        sku = _buy_fungible(conn, "Unrelated Operational Error Widget", quantity=5, price_value=10_000)

        real_execute = conn.execute

        class _FakeUnrelatedOrig(Exception):
            pgcode = "57P03"  # cannot_connect_now — an arbitrary, unrelated SQLSTATE

        def fake_execute(clause, *args, **kwargs):
            if "INSERT INTO fungible_depletions" in str(clause):
                raise OperationalError("INSERT INTO fungible_depletions ...", {}, _FakeUnrelatedOrig())
            return real_execute(clause, *args, **kwargs)

        conn.execute = fake_execute
        try:
            with pytest.raises(OperationalError):
                deplete_fungible(conn, sku, quantity=2)
        finally:
            conn.execute = real_execute


class TestDepleteFungibleGenuineConcurrency:
    """Races two REAL threads, each on its own connection/transaction,
    both attempting to deplete more than half of a limited stock — only
    one should succeed; the loser must be cleanly rejected
    (InsufficientStockError) and its connection must remain usable
    afterward, mirroring tests/test_purchases.py's own concurrency test
    for the SKU-uniqueness backstop.
    """

    def test_two_connections_racing_on_the_same_limited_stock(self, engine):
        with engine.begin() as setup_conn:
            from inventory.seed import seed_categories

            seed_categories(setup_conn)
            purchase_result = save_purchase(
                setup_conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Limited stock for the race",
                    total_amount_paid=Decimal("1000000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=NewItemInput(
                                name="Race Condition Widget", category_code="AUTOMOTIVE", identity_mode="fungible"
                            ),
                            quantity=10,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("1000000"),
                        )
                    ],
                ),
            )
        sku = purchase_result.lines[0].sku

        first_inserted = threading.Event()
        results: dict = {}

        def thread_a():
            conn_a = engine.connect()
            try:
                deplete_fungible(conn_a, sku, quantity=6)
                first_inserted.set()
                # Give thread B time to run its own (unsuspecting)
                # pre-check — which won't see this uncommitted depletion —
                # and reach the blocking trigger before we release the lock.
                time.sleep(0.75)
                conn_a.commit()
                results["a_ok"] = True
            finally:
                conn_a.close()

        def thread_b():
            if not first_inserted.wait(timeout=5):
                results["b_error"] = RuntimeError("thread_a never signaled")
                return
            conn_b = engine.connect()
            try:
                try:
                    deplete_fungible(conn_b, sku, quantity=6)
                    results["b_error"] = None
                except InsufficientStockError as exc:
                    results["b_error"] = exc
                    try:
                        conn_b.execute(text("SELECT 1")).scalar_one()
                        results["b_conn_usable"] = True
                    except Exception as usability_exc:  # pragma: no cover
                        results["b_conn_usable"] = False
                        results["b_usability_error"] = usability_exc
            finally:
                conn_b.rollback()
                conn_b.close()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        assert results.get("a_ok") is True
        assert isinstance(results.get("b_error"), InsufficientStockError), results.get("b_error")
        assert results.get("b_conn_usable") is True, results.get("b_usability_error")

        with engine.connect() as verify_conn:
            row = verify_conn.execute(
                text("SELECT COALESCE(SUM(quantity), 0) FROM fungible_depletions fd "
                     "JOIN items i ON i.id = fd.item_id WHERE i.sku = :sku"),
                {"sku": sku},
            ).scalar_one()
            assert row == 6, "Only the winning depletion (6 units) should have posted, never both (would be 12 > 10)."


def _fresh_fungible_stock(engine, quantity=10, price_value=100_000, name="Barrier Race Widget"):
    """A brand-new fungible item with ``quantity`` units on hand, for a
    genuinely-simultaneous race test that needs a clean, isolated item per
    trial (so one trial's outcome can never influence the next).
    """
    with engine.begin() as setup_conn:
        from inventory.seed import seed_categories

        seed_categories(setup_conn)
        purchase_result = save_purchase(
            setup_conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Fresh stock for a barrier-synchronized race trial",
                total_amount_paid=Decimal(price_value) * quantity,
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=NewItemInput(name=name, category_code="AUTOMOTIVE", identity_mode="fungible"),
                        quantity=quantity,
                        pricing_mode="direct",
                        price_entry_mode="per_unit",
                        price_value=Decimal(price_value),
                    )
                ],
            ),
        )
    return purchase_result.lines[0].sku


def _run_barrier_synchronized_pair(engine, sku, qty_a, qty_b):
    """Races two REAL threads, each on its own connection, through
    ``deplete_fungible`` at the EXACT same instant via ``threading.Barrier``
    — deliberately NOT the event-staggered "let A finish first" pattern
    used by the test above. This is QA's own repro methodology (see the
    bug report this fixes): a `threading.Event`-based stagger only proves
    the loser of an already-decided race gets a clean rejection; it can
    never expose a lock-ACQUISITION-ORDER deadlock, which specifically
    requires both transactions to be genuinely in-flight and contending
    for the same lock at once. Returns a dict of {"a": outcome, "b": outcome}
    where outcome is one of: "success", "insufficient", or a string
    starting with "UNEXPECTED:" for anything else (a deadlock, a
    DepletionConflictError, or any other error — all treated as a hard
    failure by the caller, since none of those are acceptable outcomes for
    two individually-well-formed requests).
    """
    barrier = threading.Barrier(2)
    results: dict = {}

    def racer(name: str, qty: int):
        conn = engine.connect()
        try:
            barrier.wait(timeout=5)
            try:
                deplete_fungible(conn, sku, quantity=qty)
                conn.commit()
                results[name] = "success"
            except InsufficientStockError:
                conn.rollback()
                results[name] = "insufficient"
            except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
                conn.rollback()
                results[name] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
        finally:
            conn.close()

    ta = threading.Thread(target=racer, args=("a", qty_a))
    tb = threading.Thread(target=racer, args=("b", qty_b))
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    return results


class TestDepleteFungibleGenuineSimultaneousConcurrency:
    """QA-found bug, fixed 2026-09-10: the trigger's original `SELECT ...
    FOR UPDATE` row lock deadlocked under genuinely-simultaneous
    concurrency (a real, reproducible `psycopg2.errors.DeadlockDetected`,
    surfacing as an uncaught `OperationalError` instead of the clean
    `InsufficientStockError` the design promises) — root-caused to a
    lock-upgrade conflict against the row's own foreign-key-driven `FOR
    KEY SHARE` lock (see migrations/003_add_depletions.sql's trigger
    comment for the full analysis). Fixed by switching to
    `pg_advisory_xact_lock`, a lock space wholly independent of the FK
    lock, so there's no shared resource for two transactions to deadlock
    over. These tests use the SAME `threading.Barrier` methodology QA used
    to find the bug (genuinely simultaneous, not event-staggered — see
    `_run_barrier_synchronized_pair`'s docstring), run across many trials
    each, covering both scenarios QA's report called out.
    """

    TRIALS = 25

    def test_genuinely_simultaneous_over_commit_never_deadlocks(self, engine):
        """Stock=10, both threads request 6 (12 > 10 — exactly one MUST be
        rejected). QA found 6/8 trials of this exact shape produced an
        uncaught deadlock before the fix.
        """
        unexpected = []
        for _ in range(self.TRIALS):
            sku = _fresh_fungible_stock(engine, quantity=10)
            results = _run_barrier_synchronized_pair(engine, sku, qty_a=6, qty_b=6)
            outcomes = sorted(results.values())
            if any(v.startswith("UNEXPECTED") for v in outcomes):
                unexpected.append(outcomes)
                continue
            assert outcomes == ["insufficient", "success"], outcomes

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials produced an unexpected outcome "
            f"(deadlock or other uncaught error) instead of a clean success/rejection pair: {unexpected}"
        )

    def test_genuinely_simultaneous_both_requests_individually_valid_never_deadlocks(self, engine):
        """Stock=10, both threads request only 3 each (6 total — well
        within capacity, BOTH requests should always succeed). QA found
        this was the more serious finding: 7/10 trials produced an
        uncaught error for one of the two individually-valid requests,
        purely from lock-acquisition timing — nothing to do with actually
        running out of stock.
        """
        unexpected = []
        for _ in range(self.TRIALS):
            sku = _fresh_fungible_stock(engine, quantity=10)
            results = _run_barrier_synchronized_pair(engine, sku, qty_a=3, qty_b=3)
            outcomes = sorted(results.values())
            if outcomes != ["success", "success"]:
                unexpected.append(outcomes)

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials failed to let BOTH individually-valid "
            f"requests succeed (expected two clean successes every time): {unexpected}"
        )


class TestDepleteSerialUnitNormalPath:
    def test_normal_depletion_marks_unit_sold_with_its_own_cost(self, conn):
        sku, serials = _buy_serialized(conn, "Omega Speedmaster", quantity=1, total_price=15_000_000)
        result = deplete_serial_unit(conn, serials[0], expected_sku=sku, reference="Sold via eBay")

        assert result.serial_id == serials[0]
        assert result.acquired_cost == Decimal("15000000")
        assert result.reference == "Sold via eBay"

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 0
        assert stats.cost_basis == Decimal("0")

    def test_depleting_one_of_several_units_leaves_the_rest_on_hand(self, conn):
        sku, serials = _buy_serialized(conn, "Rolex Submariner", quantity=3, total_price=30_000_000)
        deplete_serial_unit(conn, serials[1])

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 2
        assert stats.cost_basis == Decimal("20000000")


class TestDepleteSerialUnitRejections:
    def test_nonexistent_serial_id_rejected(self, conn):
        with pytest.raises(SerialUnitNotFoundError):
            deplete_serial_unit(conn, "NOPE-DOES-NOT-EXIST-001")

    def test_already_sold_unit_rejected(self, conn):
        sku, serials = _buy_serialized(conn, "ECU Unit", quantity=1, total_price=5_000_000)
        deplete_serial_unit(conn, serials[0])
        with pytest.raises(AlreadyDepletedError):
            deplete_serial_unit(conn, serials[0])

    def test_wrong_item_claim_rejected_and_unit_remains_on_hand(self, conn):
        sku_a, serials_a = _buy_serialized(conn, "Watch A", quantity=1, total_price=1_000_000)
        sku_b, _ = _buy_serialized(conn, "Watch B", quantity=1, total_price=1_000_000)

        with pytest.raises(ItemMismatchError):
            deplete_serial_unit(conn, serials_a[0], expected_sku=sku_b)

        # The unit was NOT depleted — the atomic UPDATE's WHERE clause
        # never matched, so nothing changed.
        stats = get_item_stats(conn, sku_a)
        assert stats.quantity == 1

    def test_blank_serial_id_rejected(self, conn):
        with pytest.raises(ValidationError):
            deplete_serial_unit(conn, "   ")


class TestDepleteSerialUnitGenuineConcurrency:
    """Races two real threads attempting to deplete the SAME single unit —
    exactly the brief's "sell the last unit" scenario. Only one may
    succeed; this is guaranteed by the atomic compare-and-swap UPDATE
    (WHERE status = 'on_hand'), not any Python-level locking.
    """

    def test_two_connections_racing_on_the_same_single_unit(self, engine):
        with engine.begin() as setup_conn:
            from inventory.seed import seed_categories

            seed_categories(setup_conn)
            purchase_result = save_purchase(
                setup_conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Single unit for the race",
                    total_amount_paid=Decimal("1000000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=NewItemInput(
                                name="Last Unit Watch", category_code="WATCHES", identity_mode="serialized"
                            ),
                            quantity=1,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("1000000"),
                        )
                    ],
                ),
            )
        serial_id = purchase_result.lines[0].serial_ids[0]

        results: dict = {}
        barrier = threading.Barrier(2, timeout=10)

        def racer(name: str):
            conn = engine.connect()
            try:
                barrier.wait()
                try:
                    deplete_serial_unit(conn, serial_id)
                    conn.commit()
                    results[name] = "success"
                except AlreadyDepletedError:
                    conn.rollback()
                    results[name] = "rejected"
            finally:
                conn.close()

        ta = threading.Thread(target=racer, args=("a",))
        tb = threading.Thread(target=racer, args=("b",))
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        outcomes = sorted(results.values())
        assert outcomes == ["rejected", "success"], results

        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text("SELECT COUNT(*) FROM serial_units WHERE serial_id = :sid AND status = 'sold'"),
                {"sid": serial_id},
            ).scalar_one()
            assert count == 1


class TestPurchaseHistoryUnaffectedByLaterDepletion:
    """CLAUDE.md's brief: "a purchase's own allocated_item_cost/
    shipping_share/line_total per line should NOT change due to a later
    depletion (depletion is a separate, later event, not a retroactive
    edit to purchase history)."
    """

    def test_purchase_detail_figures_identical_before_and_after_depletion(self, conn):
        purchase = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Toko Grosir Jaya — pre-depletion snapshot",
                total_amount_paid=Decimal("1150000"),
                shipping_mode="pooled",
                pooled_shipping_total=Decimal("150000"),
                pooled_shipping_method="equal",
                lines=[
                    PurchaseLineInput(
                        new_item=NewItemInput(name="Brake Pad Set", category_code="AUTOMOTIVE", identity_mode="fungible"),
                        quantity=10,
                        pricing_mode="direct",
                        price_entry_mode="per_unit",
                        price_value=Decimal("100000"),
                    )
                ],
            ),
        )
        sku = purchase.lines[0].sku
        ref = purchase.purchase_ref

        before = get_purchase_detail(conn, ref)
        before_line = before.lines[0]

        deplete_fungible(conn, sku, quantity=4, reference="post-purchase sale")

        after = get_purchase_detail(conn, ref)
        after_line = after.lines[0]

        assert after_line.allocated_item_cost == before_line.allocated_item_cost == Decimal("1000000")
        assert after_line.shipping_share == before_line.shipping_share == Decimal("150000")
        assert after_line.line_total == before_line.line_total == Decimal("1150000")
        assert after_line.quantity == before_line.quantity == 10

    def test_csv_export_rows_identical_before_and_after_depletion(self, conn):
        purchase = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Export snapshot vendor",
                total_amount_paid=Decimal("500000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=NewItemInput(name="Spark Plug", category_code="AUTOMOTIVE", identity_mode="fungible"),
                        quantity=5,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("500000"),
                    )
                ],
            ),
        )
        sku = purchase.lines[0].sku

        before_rows = build_export_rows(conn, purchase_ids=[purchase.id])
        deplete_fungible(conn, sku, quantity=2)
        after_rows = build_export_rows(conn, purchase_ids=[purchase.id])

        assert len(before_rows) == len(after_rows) == 1
        assert before_rows[0].allocated_item_cost == after_rows[0].allocated_item_cost == Decimal("500000")
        assert before_rows[0].line_total == after_rows[0].line_total == Decimal("500000")
        assert before_rows[0].unit_cost == after_rows[0].unit_cost == Decimal("100000")
        assert before_rows[0].quantity == after_rows[0].quantity == 5
