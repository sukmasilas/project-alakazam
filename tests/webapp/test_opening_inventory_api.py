"""Opening-inventory save round trip through the real HTTP/JSON layer — the
endpoint must actually reach inventory.opening_inventory.record_opening_inventory()
(which itself calls the real, unmodified inventory.purchases.save_purchase())
and land in Postgres as a real Purchase, same discipline as
tests/webapp/test_purchases_api.py.
"""
from __future__ import annotations

from sqlalchemy import text


def test_new_fungible_item_saves_through_real_engine(client, engine):
    body = {
        "quantity": 5,
        "price_entry_mode": "per_unit",
        "price_value": 20000,
        "new_item": {"name": "Existing Stock Widget", "category_code": "OTHERS", "identity_mode": "fungible"},
    }
    resp = client.post("/api/opening-inventory", json=body)
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["purchase_ref"].startswith("PUR-")
    assert saved["allocation"]["balanced"] is True
    assert saved["allocation"]["running_total"] == 100000
    assert len(saved["lines"]) == 1
    sku = saved["lines"][0]["sku"]

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT vendor_description, total_amount_paid FROM purchases WHERE purchase_ref = :ref"),
            {"ref": saved["purchase_ref"]},
        ).first()
    assert row is not None
    assert row[0] == "Opening Inventory"
    assert int(row[1]) == 100000

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 5
    assert detail["cost_basis"] == "100000"


def test_vendor_description_override_sticks(client):
    body = {
        "quantity": 1,
        "price_entry_mode": "total",
        "price_value": 50000,
        "new_item": {"name": "Overridden Vendor Widget", "category_code": "OTHERS", "identity_mode": "fungible"},
        "vendor_description": "Counted during physical stock take",
    }
    resp = client.post("/api/opening-inventory", json=body)
    assert resp.status_code == 201, resp.text
    ref = resp.json()["purchase_ref"]

    detail = client.get(f"/api/purchases/{ref}").json()
    assert detail["vendor_description"] == "Counted during physical stock take"


def test_new_serialized_item_multiple_units(client):
    body = {
        "quantity": 2,
        "price_entry_mode": "total",
        "price_value": 8000000,
        "new_item": {"name": "Existing Rolex Stock", "category_code": "WATCHES", "identity_mode": "serialized"},
        "serial_units": [
            {"serial_id": "OI-ROLEX-001", "cost": 3000000},
            {"serial_id": "OI-ROLEX-002", "cost": 5000000},
        ],
    }
    resp = client.post("/api/opening-inventory", json=body)
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["lines"][0]["serial_ids"] == ["OI-ROLEX-001", "OI-ROLEX-002"]


def test_existing_item_pools_via_weighted_average(client):
    created = client.post(
        "/api/purchases",
        json={
            "purchase_date": "2026-06-01",
            "vendor_description": "Original vendor",
            "total_amount_paid": 100000,
            "currency": "IDR",
            "shipping_mode": "none",
            "lines": [
                {
                    "new_item": {"name": "Existing Pooled Item", "category_code": "OTHERS", "identity_mode": "fungible"},
                    "quantity": 10,
                    "pricing_mode": "direct",
                    "price_entry_mode": "total",
                    "price_value": 100000,
                }
            ],
        },
    ).json()
    sku = created["lines"][0]["sku"]

    resp = client.post(
        "/api/opening-inventory",
        json={"quantity": 6, "price_entry_mode": "total", "price_value": 120000, "sku": sku},
    )
    assert resp.status_code == 201, resp.text

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 16
    assert detail["cost_basis"] == "220000"


def test_missing_item_selection_rejected_by_real_server_check(client):
    resp = client.post(
        "/api/opening-inventory",
        json={"quantity": 1, "price_entry_mode": "total", "price_value": 1000},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ValidationError"
