"""Sale-side depletion reached through the real HTTP/JSON layer (Milestone
5) — proves the Mark-as-Sold action actually calls the real
inventory.depletions engine (not a separate, weaker web-layer check), and
that over-depletion is rejected server-side even for a client that lies
about it.
"""
from __future__ import annotations

from sqlalchemy import text


def _post_fungible_purchase(client, name, quantity, price_value, category_code="AUTOMOTIVE"):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": f"Stock-up: {name}",
        "total_amount_paid": quantity * price_value,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": quantity,
                "pricing_mode": "direct",
                "price_entry_mode": "per_unit",
                "price_value": price_value,
                "ships_separately": False,
                "new_item": {"name": name, "category_code": category_code, "identity_mode": "fungible"},
            }
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["lines"][0]["sku"]


def _post_serialized_purchase(client, name, total_price, serial_id, category_code="WATCHES"):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": f"Stock-up: {name}",
        "total_amount_paid": total_price,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": 1,
                "pricing_mode": "direct",
                "price_entry_mode": "total",
                "price_value": total_price,
                "ships_separately": False,
                "new_item": {"name": name, "category_code": category_code, "identity_mode": "serialized"},
                "serial_units": [{"serial_id": serial_id}],
            }
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["lines"][0]["sku"]


def test_deplete_fungible_through_real_engine(client, engine):
    sku = _post_fungible_purchase(client, "Brake Pad Set", quantity=10, price_value=100_000)

    resp = client.post(f"/api/items/{sku}/deplete", json={"quantity": 4, "reference": "eBay #999"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity"] == 4
    # total_cost is a plain (whole-rupiah) int field on FungibleDepletionResult
    # (round_half_up already returns int) — same wire convention already
    # established by inventory.purchases.SavedLine's own int fields (e.g.
    # PurchaseAllocation.running_total), which the existing test suite
    # already asserts as plain JSON numbers, not strings. unit_cost IS a
    # Decimal (a genuinely fractional weighted-average ratio) and so DOES
    # get the string treatment, per api.py's "never float" rule.
    assert body["total_cost"] == 400000
    assert body["unit_cost"] == "100000"
    assert body["reference"] == "eBay #999"

    # Real row landed in Postgres, not just a JSON response.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT quantity, total_cost, reference FROM fungible_depletions fd "
                 "JOIN items i ON i.id = fd.item_id WHERE i.sku = :sku"),
            {"sku": sku},
        ).mappings().first()
    assert row is not None
    assert int(row["quantity"]) == 4
    assert int(row["total_cost"]) == 400_000
    assert row["reference"] == "eBay #999"

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 6
    assert detail["cost_basis"] == "600000"


def test_deplete_fungible_over_depletion_rejected_at_api_level(client, engine):
    """Server-side gate — a client claiming it wants to deplete more than
    is on hand must be rejected by the real API, with nothing written.
    """
    sku = _post_fungible_purchase(client, "Turbocharger Kit", quantity=3, price_value=500_000)

    resp = client.post(f"/api/items/{sku}/deplete", json={"quantity": 999})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "InsufficientStockError"

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM fungible_depletions")).scalar_one()
    assert count == 0, "An over-depletion request must never partially post."

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 3


def test_deplete_fungible_zero_quantity_rejected(client):
    sku = _post_fungible_purchase(client, "Oil Filter", quantity=5, price_value=50_000)
    resp = client.post(f"/api/items/{sku}/deplete", json={"quantity": 0})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ValidationError"


def test_deplete_serial_unit_through_real_engine(client, engine):
    sku = _post_serialized_purchase(client, "Omega Speedmaster", 15_000_000, "OMEGA-API-001")

    resp = client.post(
        f"/api/items/{sku}/serial-units/OMEGA-API-001/deplete", json={"reference": "Sold via eBay"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["serial_id"] == "OMEGA-API-001"
    assert body["acquired_cost"] == "15000000"

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, sold_reference FROM serial_units WHERE serial_id = 'OMEGA-API-001'")
        ).mappings().first()
    assert row["status"] == "sold"
    assert row["sold_reference"] == "Sold via eBay"

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 0
    unit = detail["serialized_units"][0]
    assert unit["status"] == "sold"


def test_deplete_serial_unit_already_sold_rejected(client):
    sku = _post_serialized_purchase(client, "Rolex Submariner", 30_000_000, "ROLEX-API-001")
    first = client.post(f"/api/items/{sku}/serial-units/ROLEX-API-001/deplete", json={})
    assert first.status_code == 200

    second = client.post(f"/api/items/{sku}/serial-units/ROLEX-API-001/deplete", json={})
    assert second.status_code == 400
    assert second.json()["detail"]["error_type"] == "AlreadyDepletedError"


def test_deplete_serial_unit_wrong_item_claim_rejected(client):
    sku_a = _post_serialized_purchase(client, "Watch A", 1_000_000, "WATCHA-API-001")
    sku_b = _post_serialized_purchase(client, "Watch B", 1_000_000, "WATCHB-API-001")

    resp = client.post(f"/api/items/{sku_b}/serial-units/WATCHA-API-001/deplete", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ItemMismatchError"

    # Still on hand under its real item.
    detail = client.get(f"/api/items/{sku_a}").json()
    assert detail["quantity"] == 1


def test_deplete_nonexistent_serial_unit_rejected(client):
    sku = _post_serialized_purchase(client, "Watch C", 1_000_000, "WATCHC-API-001")
    resp = client.post(f"/api/items/{sku}/serial-units/DOES-NOT-EXIST/deplete", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "SerialUnitNotFoundError"


def test_depletions_log_endpoint_lists_both_kinds_newest_first(client):
    fungible_sku = _post_fungible_purchase(client, "Spark Plug", quantity=20, price_value=25_000)
    serial_sku = _post_serialized_purchase(client, "ECU Unit", 5_000_000, "ECU-API-001", category_code="AUTOMOTIVE")

    client.post(f"/api/items/{fungible_sku}/deplete", json={"quantity": 5, "reference": "batch sale"})
    client.post(f"/api/items/{serial_sku}/serial-units/ECU-API-001/deplete", json={"reference": "single sale"})

    listing = client.get("/api/depletions").json()
    assert len(listing) == 2
    types = {row["depletion_type"] for row in listing}
    assert types == {"fungible", "serialized"}
    for row in listing:
        assert isinstance(row["total_cost"], str)
        assert isinstance(row["unit_cost"], str)

    filtered = client.get(f"/api/depletions?sku={fungible_sku}").json()
    assert len(filtered) == 1
    assert filtered[0]["sku"] == fungible_sku


def test_sales_log_page_renders(client):
    resp = client.get("/sales")
    assert resp.status_code == 200
    assert "Sales / Depletion Log" in resp.text
    assert "sales_log.js" in resp.text


def test_purchase_history_unaffected_by_depletion_through_api(client):
    """The same invariant CLAUDE.md's brief calls out, exercised through
    the real HTTP layer this time: a purchase's own recorded line figures
    must never change because of a later, separate depletion event.
    """
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Snapshot vendor",
        "total_amount_paid": 1000000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": 10,
                "pricing_mode": "direct",
                "price_entry_mode": "per_unit",
                "price_value": 100000,
                "ships_separately": False,
                "new_item": {"name": "Snapshot Widget", "category_code": "OTHERS", "identity_mode": "fungible"},
            }
        ],
    }
    saved = client.post("/api/purchases", json=body).json()
    ref = saved["purchase_ref"]
    sku = saved["lines"][0]["sku"]

    before = client.get(f"/api/purchases/{ref}").json()
    client.post(f"/api/items/{sku}/deplete", json={"quantity": 4})
    after = client.get(f"/api/purchases/{ref}").json()

    assert before["lines"][0]["allocated_item_cost"] == after["lines"][0]["allocated_item_cost"] == "1000000"
    assert before["lines"][0]["line_total"] == after["lines"][0]["line_total"] == "1000000"
