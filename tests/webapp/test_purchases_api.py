"""Purchase save round trip through the real HTTP/JSON layer — the whole
point of this milestone: a balanced purchase must actually reach
inventory.purchases.save_purchase() and land in Postgres, and an
out-of-balance purchase must be rejected by the REAL server-side
reconciliation check, not a client-side illusion the server just trusts.
"""
from __future__ import annotations

from sqlalchemy import text


def _fungible_line(sku=None, new_item=None, quantity=10, price_value=1000, price_entry_mode="per_unit"):
    line = {
        "quantity": quantity,
        "pricing_mode": "direct",
        "price_entry_mode": price_entry_mode,
        "price_value": price_value,
        "ships_separately": False,
    }
    if sku:
        line["sku"] = sku
    else:
        line["new_item"] = new_item
    return line


def test_balanced_purchase_saves_through_real_engine(client, engine):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Toko Grosir Jaya",
        "total_amount_paid": 10000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Hot Wheels 2024", "category_code": "TOYS_COLLECTIBLES", "identity_mode": "fungible"},
                quantity=10, price_value=1000,
            )
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["purchase_ref"].startswith("PUR-2026-")
    assert saved["allocation"]["balanced"] is True
    assert saved["allocation"]["running_total"] == 10000

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT total_amount_paid FROM purchases WHERE purchase_ref = :ref"),
            {"ref": saved["purchase_ref"]},
        ).first()
    assert row is not None
    assert int(row[0]) == 10000


def test_out_of_balance_purchase_rejected_by_real_server_check(client, engine):
    """Even if a hypothetical malicious/broken client claims a purchase is
    balanced, the wire payload here deliberately does NOT balance —
    proving the server (not client JS) is the actual gate.
    """
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Mismatched Total Vendor",
        "total_amount_paid": 999999,  # does not match the line total below
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Mismatch Item", "category_code": "OTHERS", "identity_mode": "fungible"},
                quantity=1, price_value=1000,
            )
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ReconciliationError"

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM purchases")).scalar_one()
        item_count = conn.execute(
            text("SELECT COUNT(*) FROM items WHERE name = 'Mismatch Item'")
        ).scalar_one()
    assert count == 0, "A rejected purchase must not partially post."
    assert item_count == 0, "A rejected purchase's new item must not be created either."


def test_duplicate_sku_within_purchase_rejected(client):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Two lines, same new SKU",
        "total_amount_paid": 2000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Dup Item", "category_code": "OTHERS", "identity_mode": "fungible", "sku": "OTH-DUP-0001"},
                quantity=1, price_value=1000,
            ),
            _fungible_line(
                new_item={"name": "Dup Item Again", "category_code": "OTHERS", "identity_mode": "fungible", "sku": "OTH-DUP-0001"},
                quantity=1, price_value=1000,
            ),
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "DuplicateSkuError"


def test_duplicate_sku_against_existing_item_rejected(client):
    client.post("/api/items", json={"name": "Existing Item", "category_code": "OTHERS", "identity_mode": "fungible", "sku": "OTH-EXIST-0001"})
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Reuses an existing SKU as if new",
        "total_amount_paid": 1000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Existing Item", "category_code": "OTHERS", "identity_mode": "fungible", "sku": "OTH-EXIST-0001"},
                quantity=1, price_value=1000,
            )
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "DuplicateSkuError"


def test_serialized_purchase_generates_real_serials_and_enforces_uniqueness(client):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Watch pair",
        "total_amount_paid": 20000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": 2,
                "pricing_mode": "direct",
                "price_entry_mode": "total",
                "price_value": 20000,
                "ships_separately": False,
                "new_item": {"name": "Seiko Presage", "category_code": "WATCHES", "identity_mode": "serialized"},
                "serial_units": [{"serial_id": "SEIKO-001"}, {"serial_id": "SEIKO-002"}],
            }
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert sorted(saved["lines"][0]["serial_ids"]) == ["SEIKO-001", "SEIKO-002"]

    # Real uniqueness check endpoint now reflects the posted serials.
    check = client.get("/api/serials/check?serial_id=SEIKO-001").json()
    assert check["exists"] is True

    # A second purchase reusing one of those serial IDs must be rejected —
    # the real backstop (DB unique index + inventory.uniqueness), not a
    # client-side generator convenience.
    dupe_body = {
        "purchase_date": "2026-09-09",
        "vendor_description": "Reuses a posted serial",
        "total_amount_paid": 10000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": 1,
                "pricing_mode": "direct",
                "price_entry_mode": "total",
                "price_value": 10000,
                "ships_separately": False,
                "new_item": {"name": "Another Seiko", "category_code": "WATCHES", "identity_mode": "serialized"},
                "serial_units": [{"serial_id": "SEIKO-001"}],
            }
        ],
    }
    dupe_resp = client.post("/api/purchases", json=dupe_body)
    assert dupe_resp.status_code == 400
    assert dupe_resp.json()["detail"]["error_type"] == "DuplicateSerialError"


def test_purchase_history_list_and_detail(client):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "History Vendor",
        "total_amount_paid": 5000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "History Item", "category_code": "OTHERS", "identity_mode": "fungible"},
                quantity=5, price_value=1000,
            )
        ],
    }
    saved = client.post("/api/purchases", json=body).json()
    ref = saved["purchase_ref"]

    listing = client.get("/api/purchases").json()
    assert any(p["purchase_ref"] == ref for p in listing)

    detail = client.get(f"/api/purchases/{ref}").json()
    assert detail["vendor_description"] == "History Vendor"
    assert detail["lines"][0]["sku"].startswith("OTH-HISTORY-ITEM")

    missing = client.get("/api/purchases/PUR-2026-9999")
    assert missing.status_code == 404


def test_item_detail_shows_purchase_history_row(client):
    body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Detail Vendor",
        "total_amount_paid": 3000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Detail Item", "category_code": "OTHERS", "identity_mode": "fungible"},
                quantity=3, price_value=1000,
            )
        ],
    }
    saved = client.post("/api/purchases", json=body).json()
    sku = saved["lines"][0]["sku"]

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 3
    assert detail["cost_basis"] == "3000"
    assert len(detail["fungible_rows"]) == 1
    assert detail["fungible_rows"][0]["purchase_ref"] == saved["purchase_ref"]


def test_item_detail_money_fields_are_strings_not_floats(client):
    """Regression test (QA-found bug, 2026-09-10): GET /api/items/{sku} was
    leaking raw JSON floats for money fields nested inside hand-built dict
    payloads (fungible_rows / serialized_units), even though the
    equivalent purchase-detail endpoint correctly rendered strings.
    Deliberately uses a quantity that does NOT divide the line total
    evenly (10000 / 3), so allocated_unit_cost is a genuinely fractional
    Decimal — the class of value that would silently become a JSON float
    (e.g. 3333.333333...) if the string-conversion were skipped, which a
    same-quantity/evenly-divisible test can't catch.
    """
    fungible_body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Fractional Unit Cost Vendor",
        "total_amount_paid": 10000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            _fungible_line(
                new_item={"name": "Fraction Item", "category_code": "OTHERS", "identity_mode": "fungible"},
                quantity=3, price_value=10000, price_entry_mode="total",
            )
        ],
    }
    saved = client.post("/api/purchases", json=fungible_body).json()
    sku = saved["lines"][0]["sku"]

    detail = client.get(f"/api/items/{sku}").json()
    row = detail["fungible_rows"][0]
    assert isinstance(row["line_total"], str)
    assert isinstance(row["allocated_unit_cost"], str)
    assert row["line_total"] == "10000"
    assert row["allocated_unit_cost"] == "3333.333333333333333333333333"

    serialized_body = {
        "purchase_date": "2026-09-08",
        "vendor_description": "Serialized Detail Vendor",
        "total_amount_paid": 5000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "quantity": 1,
                "pricing_mode": "direct",
                "price_entry_mode": "total",
                "price_value": 5000,
                "ships_separately": False,
                "new_item": {"name": "Fraction Watch", "category_code": "WATCHES", "identity_mode": "serialized"},
                "serial_units": [{"serial_id": "FRAC-001"}],
            }
        ],
    }
    saved_serialized = client.post("/api/purchases", json=serialized_body).json()
    serialized_sku = saved_serialized["lines"][0]["sku"]

    serialized_detail = client.get(f"/api/items/{serialized_sku}").json()
    unit = serialized_detail["serialized_units"][0]
    assert isinstance(unit["cost"], str)
    assert unit["cost"] == "5000"
