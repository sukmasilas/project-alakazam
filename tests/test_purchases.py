"""Integration tests for inventory/purchases.py against a real Postgres
database — the reconciliation invariant and SKU/serial uniqueness are
enforced partly by real DB constraints (migrations/001_initial_schema.sql),
so these need the real engine, not a mock.
"""
from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inventory.exceptions import (
    DuplicateSerialError,
    DuplicateSkuError,
    ReconciliationError,
    ValidationError,
)
from inventory.purchases import (
    NewItemInput,
    PurchaseInput,
    PurchaseLineInput,
    SerialUnitInput,
    save_purchase,
)
from inventory.queries import get_item_stats
from inventory.seed import seed_categories


def _new_fungible(name, category_code="AUTOMOTIVE", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="fungible", sku=sku)


def _new_serialized(name, category_code="AUTOMOTIVE", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="serialized", sku=sku)


class TestMultiLineMixedIdentityPooledByWeight:
    """A multi-line purchase mixing a fungible and a serialized item, with
    pooled shipping split by weight — mirrors PUR-2026-0083 from the mockup
    sample data.
    """

    def test_saves_and_reconciles(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 7, 14),
            vendor_description="Toko Grosir Jaya — bulk auto parts + Hot Wheels case lot",
            total_amount_paid=Decimal("11540000"),
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("400000"),
            pooled_shipping_method="by_weight",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Brake Pad Set Toyota"),
                    quantity=56,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("65000"),
                    shipping_weight_kg=Decimal("25"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Hot Wheels Mainline", category_code="TOYS_COLLECTIBLES"),
                    quantity=180,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("20000"),
                    shipping_weight_kg=Decimal("15"),
                ),
                PurchaseLineInput(
                    new_item=_new_serialized("ECU Unit Honda"),
                    quantity=4,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("3600000"),
                    ships_separately=True,
                    manual_shipping_amount=Decimal("300000"),
                ),
            ],
        )
        result = save_purchase(conn, purchase)

        assert result.allocation.balanced
        assert result.allocation.running_total == 11_540_000

        ecu_line = result.lines[2]
        assert len(ecu_line.serial_ids) == 4
        assert len(set(ecu_line.serial_ids)) == 4  # all unique
        assert ecu_line.serial_ids[0].startswith(ecu_line.sku)

        brakepad_line = result.lines[0]
        hotwheels_line = result.lines[1]
        assert brakepad_line.shipping_share == 250_000
        assert hotwheels_line.shipping_share == 150_000
        assert ecu_line.shipping_share == 300_000

        # On-hand rollups trace back correctly.
        brakepad_stats = get_item_stats(conn, brakepad_line.sku)
        assert brakepad_stats.quantity == 56
        assert brakepad_stats.cost_basis == Decimal(brakepad_line.line_total)

        ecu_stats = get_item_stats(conn, ecu_line.sku)
        assert ecu_stats.quantity == 4
        assert ecu_stats.cost_basis == Decimal(ecu_line.line_total)


class TestLumpSumFallbackMixedWithDirectPricing:
    """Mirrors PUR-2026-0102: a lump-sum group (By Value) alongside
    directly-priced lines in the same purchase.
    """

    def test_lump_sum_lines_and_direct_lines_coexist(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 2),
            vendor_description="Toko Aksesoris Klasik — mixed lot, single bundled price",
            total_amount_paid=Decimal("4380000"),
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("180000"),
            pooled_shipping_method="equal",
            lump_sum_active=True,
            lump_sum_total=Decimal("4000000"),
            lump_sum_method="by_value",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Antique Coin Set", category_code="OTHERS"),
                    quantity=10,
                    pricing_mode="lumpsum_group",
                    lumpsum_value=Decimal("60"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Diecast Truck Lot", category_code="TOYS_COLLECTIBLES"),
                    quantity=15,
                    pricing_mode="lumpsum_group",
                    lumpsum_value=Decimal("40"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Vinyl Record Lot", category_code="OTHERS"),
                    quantity=5,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("200000"),
                ),
            ],
        )
        result = save_purchase(conn, purchase)
        assert result.allocation.balanced

        coins, trucks, vinyl = result.lines
        assert coins.allocated_item_cost == 2_400_000
        assert trucks.allocated_item_cost == 1_600_000
        assert vinyl.allocated_item_cost == 200_000


class TestShipsSeparatelyOptOut:
    def test_line_opts_out_of_pooled_shipping(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 6, 20),
            vendor_description="Loko Jaya Watch Traders",
            total_amount_paid=Decimal("2050000"),
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("50000"),
            pooled_shipping_method="equal",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Watch Strap Assorted", category_code="WATCHES"),
                    quantity=10,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("100000"),
                ),
                PurchaseLineInput(
                    new_item=_new_serialized("Omega Speedmaster", category_code="WATCHES"),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("1000000"),
                    ships_separately=True,
                    manual_shipping_amount=Decimal("0"),
                ),
            ],
        )
        result = save_purchase(conn, purchase)
        assert result.allocation.balanced
        strap_line, watch_line = result.lines
        assert strap_line.shipping_share == 50_000
        assert watch_line.shipping_share == 0


class TestShippingModeNone:
    def test_no_shipping_mode_gives_every_line_zero_shipping(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 8, 5),
            vendor_description="Pasar Barang Antik — vintage poster lot",
            total_amount_paid=Decimal("3600000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Vintage Movie Poster Lot", category_code="OTHERS"),
                    quantity=12,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("300000"),
                )
            ],
        )
        result = save_purchase(conn, purchase)
        assert result.allocation.balanced
        assert result.lines[0].shipping_share == 0


class TestPriceEntryModeToggleConversionRoundTrip:
    def test_per_unit_entry_persists_the_exact_total(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 8, 22),
            vendor_description="CardVault Distro — booster restock only",
            total_amount_paid=Decimal("2000000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Pokemon Booster Pack", category_code="TCG"),
                    quantity=64,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("31250"),
                )
            ],
        )
        result = save_purchase(conn, purchase)
        assert result.lines[0].allocated_item_cost == 64 * 31250 == 2_000_000


class TestSerialUniquenessAcrossTwoLinesSameSku:
    def test_two_lines_of_same_existing_sku_generate_without_collision(self, conn):
        first = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Initial ECU batch",
                total_amount_paid=Decimal("1000000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=_new_serialized("ECU Unit Honda"),
                        quantity=2,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("1000000"),
                    )
                ],
            ),
        )
        sku = first.lines[0].sku
        assert first.lines[0].serial_ids == [f"{sku}-001", f"{sku}-002"]

        second = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 15),
                vendor_description="Second ECU batch, same SKU",
                total_amount_paid=Decimal("600000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        sku=sku,
                        quantity=1,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("600000"),
                    )
                ],
            ),
        )
        assert second.lines[0].serial_ids == [f"{sku}-003"]

    def test_two_lines_of_same_existing_sku_within_one_purchase_do_not_collide(self, conn):
        # Two lines referencing the SAME already-catalogued item within one
        # purchase (e.g. two sub-lots of the same serialized item on one
        # invoice) — this is the scenario inventory.purchases's
        # assigned_serials_by_sku bookkeeping exists for. A brand-new item
        # can only ever be created by ONE line per purchase (a second line
        # can't reference it as "existing" until it's actually saved — see
        # design doc: new items only enter the catalog at save time), so
        # this pre-registers the item first, mirroring the Inventory
        # screen's "+ Add New Item" (a real, zero-stock catalog entry).
        sku = "AUTO-TESTMULTI-0001"
        category_id = conn.execute(
            text("SELECT id FROM categories WHERE code = 'AUTOMOTIVE'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO items (sku, name, category_id, identity_mode) "
                "VALUES (:sku, 'Test Multi Unit', :cat, 'serialized')"
            ),
            {"sku": sku, "cat": category_id},
        )

        result = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="One purchase, two lines of one already-catalogued serialized SKU",
                total_amount_paid=Decimal("300000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        sku=sku,
                        quantity=2,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("200000"),
                    ),
                    PurchaseLineInput(
                        sku=sku,
                        quantity=1,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("100000"),
                    ),
                ],
            ),
        )
        all_serials = result.lines[0].serial_ids + result.lines[1].serial_ids
        assert all_serials == [f"{sku}-001", f"{sku}-002", f"{sku}-003"]
        assert len(set(all_serials)) == 3


class TestUniquenessIsRejectedNotJustAvoided:
    """The real bug the brief calls out: the generator avoids collisions in
    the common case, but a save must ALSO be blocked if a duplicate arises
    anyway (manual override, or two lines racing). These tests deliberately
    force a duplicate rather than relying on generation to avoid one.
    """

    def test_manually_typed_duplicate_sku_is_rejected_by_app_level_check(self, conn):
        save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="First item with an explicit SKU",
                total_amount_paid=Decimal("100000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=_new_fungible("Widget", sku="AUTO-WIDGET-0001"),
                        quantity=1,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("100000"),
                    )
                ],
            ),
        )

        with pytest.raises(DuplicateSkuError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 2),
                    vendor_description="A second, unrelated item — same SKU typed by hand",
                    total_amount_paid=Decimal("50000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_fungible("Completely Different Widget", sku="AUTO-WIDGET-0001"),
                            quantity=1,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("50000"),
                        )
                    ],
                ),
            )

    def test_two_new_item_lines_in_one_purchase_with_the_same_explicit_sku_is_rejected(self, conn):
        with pytest.raises(DuplicateSkuError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Two lines, same hand-typed SKU, neither posted yet",
                    total_amount_paid=Decimal("200000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_fungible("Widget A", sku="AUTO-DUPETEST-0001"),
                            quantity=1,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("100000"),
                        ),
                        PurchaseLineInput(
                            new_item=_new_fungible("Widget B", sku="AUTO-DUPETEST-0001"),
                            quantity=1,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("100000"),
                        ),
                    ],
                ),
            )

    def test_manually_assigned_duplicate_serial_is_rejected(self, conn):
        with pytest.raises(DuplicateSerialError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="One line, two units, same hand-typed serial",
                    total_amount_paid=Decimal("200000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_serialized("Dupe Serial Widget"),
                            quantity=2,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("200000"),
                            serial_units=[
                                SerialUnitInput(serial_id="DUPE-001", cost=Decimal("100000")),
                                SerialUnitInput(serial_id="DUPE-001", cost=Decimal("100000")),
                            ],
                        )
                    ],
                ),
            )

    def test_db_level_unique_index_rejects_duplicate_sku_bypassing_the_app_check(self, conn):
        """Proves the DB-level unique index is a REAL, independent
        constraint — not just relied upon via the application check above.
        Inserts directly via SQL, bypassing save_purchase entirely.
        """
        category_id = conn.execute(
            text("SELECT id FROM categories WHERE code = 'AUTOMOTIVE'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO items (sku, name, category_id, identity_mode) "
                "VALUES (:sku, 'First', :cat, 'fungible')"
            ),
            {"sku": "AUTO-RAWDUPE-0001", "cat": category_id},
        )
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO items (sku, name, category_id, identity_mode) "
                        "VALUES (:sku, 'Second', :cat, 'fungible')"
                    ),
                    {"sku": "AUTO-RAWDUPE-0001", "cat": category_id},
                )
        # Connection is still usable afterward (the savepoint absorbed the abort).
        count = conn.execute(
            text("SELECT COUNT(*) FROM items WHERE sku = 'AUTO-RAWDUPE-0001'")
        ).scalar_one()
        assert count == 1

    def test_db_level_unique_index_rejects_duplicate_serial_bypassing_the_app_check(self, conn):
        category_id = conn.execute(
            text("SELECT id FROM categories WHERE code = 'AUTOMOTIVE'")
        ).scalar_one()
        item_id = conn.execute(
            text(
                "INSERT INTO items (sku, name, category_id, identity_mode) "
                "VALUES ('AUTO-RAWSERIAL-0001', 'Raw Serial Test', :cat, 'serialized') RETURNING id"
            ),
            {"cat": category_id},
        ).scalar_one()
        purchase_id = conn.execute(
            text(
                "INSERT INTO purchases (purchase_ref, purchase_date, vendor_description, "
                "total_amount_paid, shipping_mode) VALUES "
                "('PUR-TEST-RAW', '2026-06-01', 'raw test', 100000, 'none') RETURNING id"
            )
        ).scalar_one()
        line_id = conn.execute(
            text(
                "INSERT INTO purchase_line_items (purchase_id, item_id, quantity, pricing_mode, "
                "price_entry_mode, price_value, allocated_item_cost, shipping_share, line_total) "
                "VALUES (:pid, :iid, 1, 'direct', 'total', 100000, 100000, 0, 100000) RETURNING id"
            ),
            {"pid": purchase_id, "iid": item_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO serial_units (item_id, serial_id, acquired_cost, purchase_line_item_id) "
                "VALUES (:iid, 'RAW-SERIAL-001', 100000, :lid)"
            ),
            {"iid": item_id, "lid": line_id},
        )
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO serial_units (item_id, serial_id, acquired_cost, purchase_line_item_id) "
                        "VALUES (:iid, 'RAW-SERIAL-001', 50000, :lid)"
                    ),
                    {"iid": item_id, "lid": line_id},
                )


class TestGenuineConcurrencyLeavesConnectionUsable:
    """QA finding (targeted fix, not a full re-review): the application-
    level pre-check (sku_exists / serial_exists) only catches the common,
    sequential case. Under genuine concurrency — two callers each on their
    own connection/transaction, neither having committed yet — both can
    pass the pre-check (neither sees the other's uncommitted row), and the
    real unique index is what actually catches the second INSERT. Before
    the fix, catching that IntegrityError left the whole caller-supplied
    connection hard-aborted in Postgres (InFailedSqlTransaction) even
    though the raised DuplicateSkuError/DuplicateSerialError looks
    identical to the far more common pre-check path. These tests race two
    REAL threads through save_purchase() itself (not raw SQL bypassing it
    — that path is already covered in TestUniquenessIsRejectedNotJustAvoided
    and TestReconciliationInvariant) and assert the losing connection is
    still usable immediately after the exception is caught.

    Uses the ``engine`` fixture directly (not ``conn``) — two independent,
    genuinely concurrent connections are the whole point, which the
    single-shared-open-transaction ``conn`` fixture can't provide.
    """

    def test_two_connections_racing_on_the_same_new_sku(self, engine):
        with engine.begin() as setup_conn:
            seed_categories(setup_conn)

        sku = "AUTO-RACECOND-0001"
        item_inserted = threading.Event()
        results: dict = {}

        def build_purchase() -> PurchaseInput:
            return PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Race condition test",
                total_amount_paid=Decimal("100000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=_new_fungible("Race Condition Widget", sku=sku),
                        quantity=1,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("100000"),
                    )
                ],
            )

        def thread_a():
            conn_a = engine.connect()
            try:
                save_purchase(conn_a, build_purchase())
                item_inserted.set()
                # Give thread B time to run its own (unsuspecting)
                # pre-check — which won't see this uncommitted row — and
                # reach the blocking INSERT before we release the lock.
                time.sleep(0.75)
                conn_a.commit()
            finally:
                conn_a.close()

        def thread_b():
            if not item_inserted.wait(timeout=5):
                results["b_error"] = RuntimeError("thread_a never signaled item_inserted")
                return
            conn_b = engine.connect()
            try:
                try:
                    save_purchase(conn_b, build_purchase())
                    results["b_error"] = None
                except DuplicateSkuError as exc:
                    results["b_error"] = exc
                    # The real assertion: conn_b must still be usable right
                    # after catching this, not left hard-aborted.
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

        assert isinstance(results.get("b_error"), DuplicateSkuError), results.get("b_error")
        assert results.get("b_conn_usable") is True, results.get("b_usability_error")

        # And the winner's data is exactly what's left standing.
        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text("SELECT COUNT(*) FROM items WHERE sku = :sku"), {"sku": sku}
            ).scalar_one()
            assert count == 1


class TestReconciliationInvariant:
    def test_balanced_purchase_saves_successfully(self, conn):
        result = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Balanced purchase",
                total_amount_paid=Decimal("100000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=_new_fungible("Balanced Widget"),
                        quantity=1,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("100000"),
                    )
                ],
            ),
        )
        assert result.allocation.balanced

    def test_unbalanced_purchase_is_rejected_before_any_row_is_written(self, conn):
        with pytest.raises(ReconciliationError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Unbalanced purchase — total paid does not match lines",
                    total_amount_paid=Decimal("999999"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_fungible("Mismatched Widget"),
                            quantity=1,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("100000"),
                        )
                    ],
                ),
            )
        # Nothing was written: the new item never made it into the catalog.
        exists = conn.execute(
            text("SELECT COUNT(*) FROM items WHERE name = 'Mismatched Widget'")
        ).scalar_one()
        assert exists == 0

    def test_db_level_trigger_is_a_real_independent_backstop(self, conn):
        """Bypasses save_purchase entirely (raw SQL) to prove the deferred
        constraint trigger itself rejects an unbalanced purchase at commit
        time — not just relied upon via the Python-side check above.
        Forces an immediate check with SET CONSTRAINTS ALL IMMEDIATE
        instead of actually committing (same technique used by
        Project-Noctrowl's own trigger test, for the same reason: this
        fixture's transaction is rolled back, never committed).
        """
        category_id = conn.execute(
            text("SELECT id FROM categories WHERE code = 'AUTOMOTIVE'")
        ).scalar_one()
        item_id = conn.execute(
            text(
                "INSERT INTO items (sku, name, category_id, identity_mode) "
                "VALUES ('AUTO-TRIGGERTEST-0001', 'Trigger Test', :cat, 'fungible') RETURNING id"
            ),
            {"cat": category_id},
        ).scalar_one()
        purchase_id = conn.execute(
            text(
                "INSERT INTO purchases (purchase_ref, purchase_date, vendor_description, "
                "total_amount_paid, shipping_mode) VALUES "
                "('PUR-TEST-TRIGGER', '2026-06-01', 'trigger test', 100000, 'none') RETURNING id"
            )
        ).scalar_one()

        with pytest.raises(Exception, match="out of balance"):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO purchase_line_items (purchase_id, item_id, quantity, pricing_mode, "
                        "price_entry_mode, price_value, allocated_item_cost, shipping_share, line_total) "
                        "VALUES (:pid, :iid, 1, 'direct', 'total', 50000, 50000, 0, 50000)"
                    ),
                    {"pid": purchase_id, "iid": item_id},
                )
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


class TestValidation:
    def test_serialized_line_requires_serial_units_matching_quantity(self, conn):
        with pytest.raises(ValidationError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Wrong number of serial units provided",
                    total_amount_paid=Decimal("100000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_serialized("Under-Specified Widget"),
                            quantity=2,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("100000"),
                            serial_units=[SerialUnitInput(serial_id="ONLY-ONE")],
                        )
                    ],
                ),
            )

    def test_serial_unit_costs_must_sum_to_line_total_when_explicitly_given(self, conn):
        with pytest.raises(ValidationError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Explicit per-unit costs that don't add up",
                    total_amount_paid=Decimal("100000"),
                    shipping_mode="none",
                    lines=[
                        PurchaseLineInput(
                            new_item=_new_serialized("Mismatched Cost Widget"),
                            quantity=2,
                            pricing_mode="direct",
                            price_entry_mode="total",
                            price_value=Decimal("100000"),
                            serial_units=[
                                SerialUnitInput(serial_id="A-001", cost=Decimal("40000")),
                                SerialUnitInput(serial_id="A-002", cost=Decimal("40000")),
                            ],
                        )
                    ],
                ),
            )

    def test_purchase_with_no_lines_is_rejected(self, conn):
        with pytest.raises(ValidationError):
            save_purchase(
                conn,
                PurchaseInput(
                    purchase_date=date(2026, 6, 1),
                    vendor_description="Empty purchase",
                    total_amount_paid=Decimal("0"),
                    shipping_mode="none",
                    lines=[],
                ),
            )
