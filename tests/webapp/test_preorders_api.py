"""Pre-order/dropship sales reached through the real HTTP/JSON layer
(Milestone 7) — proves the web endpoints call the real inventory.preorders
engine (not a separate, weaker web-layer check), and that the server is
always authoritative (a client-supplied sku/status is never trusted beyond
what the real engine actually accepts).
"""
from __future__ import annotations

from sqlalchemy import text


def _post_new_item(client, name, identity_mode, category_code="OTHERS"):
    resp = client.post(
        "/api/items", json={"name": name, "category_code": category_code, "identity_mode": identity_mode}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["sku"]


def _post_fungible_purchase(client, sku, quantity, price_value, vendor="Stock purchase"):
    body = {
        "purchase_date": "2026-09-14",
        "vendor_description": vendor,
        "total_amount_paid": quantity * price_value,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "sku": sku,
                "quantity": quantity,
                "pricing_mode": "direct",
                "price_entry_mode": "per_unit",
                "price_value": price_value,
                "ships_separately": False,
            }
        ],
    }
    resp = client.post("/api/purchases", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_record_preorder_sale_through_real_engine(client, engine):
    sku = _post_new_item(client, "API Preorder Widget", "fungible")

    resp = client.post(
        "/api/preorder-sales", json={"sku": sku, "quantity": 5, "sale_date": "2026-09-14", "reference": "eBay #42"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["quantity"] == 5
    assert body["reference"] == "eBay #42"

    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, quantity FROM preorder_sales WHERE id = :id"), {"id": body["id"]}).mappings().first()
    assert row["status"] == "pending"
    assert row["quantity"] == 5


def test_record_preorder_sale_for_serialized_item_rejected(client):
    sku = _post_new_item(client, "API Preorder Watch", "serialized", category_code="WATCHES")
    resp = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 1})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ValidationError"


def test_list_preorder_sales_filters_by_status(client):
    sku = _post_new_item(client, "API Preorder Filter Widget", "fungible")
    created = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 2}).json()

    all_rows = client.get("/api/preorder-sales").json()
    assert any(r["id"] == created["id"] for r in all_rows)

    client.post(f"/api/preorder-sales/{created['id']}/cancel")
    pending = client.get("/api/preorder-sales?status=pending").json()
    assert not any(r["id"] == created["id"] for r in pending)
    cancelled = client.get("/api/preorder-sales?status=cancelled").json()
    assert any(r["id"] == created["id"] for r in cancelled)


def test_cancel_preorder_sale_through_real_engine(client, engine):
    sku = _post_new_item(client, "API Preorder Cancel Widget", "fungible")
    created = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 1}).json()

    resp = client.post(f"/api/preorder-sales/{created['id']}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    # Cancelling again must fail cleanly — no reopen/reverse flow.
    resp2 = client.post(f"/api/preorder-sales/{created['id']}/cancel")
    assert resp2.status_code == 400
    assert resp2.json()["detail"]["error_type"] == "PreorderSaleNotPendingError"


def test_fulfill_preorder_sale_with_new_purchase_through_real_engine(client, engine):
    sku = _post_new_item(client, "API Preorder Fulfill Widget", "fungible")
    preorder = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 4}).json()

    body = {
        "preorder_sale_ids": [preorder["id"]],
        "purchase": {
            "purchase_date": "2026-09-14",
            "vendor_description": "Fulfilling purchase",
            "total_amount_paid": 400000,
            "currency": "IDR",
            "shipping_mode": "none",
            "lines": [
                {
                    "sku": sku,
                    "quantity": 4,
                    "pricing_mode": "direct",
                    "price_entry_mode": "per_unit",
                    "price_value": 100000,
                    "ships_separately": False,
                }
            ],
        },
    }
    resp = client.post("/api/preorder-sales/fulfill", json=body)
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert len(result["fulfilled"]) == 1
    assert result["fulfilled"][0]["preorder_sale_id"] == preorder["id"]
    assert result["fulfilled"][0]["depletion"]["total_cost"] == 400000

    detail = client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 0

    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM preorder_sales WHERE id = :id"), {"id": preorder["id"]}).scalar_one()
    assert status == "fulfilled"


def test_fulfill_preorder_sale_with_existing_purchase_id(client):
    sku = _post_new_item(client, "API Preorder Existing Purchase Widget", "fungible")
    preorder = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 3}).json()
    purchase = _post_fungible_purchase(client, sku, quantity=3, price_value=50000)

    resp = client.post(
        "/api/preorder-sales/fulfill",
        json={"preorder_sale_ids": [preorder["id"]], "purchase_id": purchase["id"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["purchase"] is None
    assert resp.json()["purchase_id"] == purchase["id"]


def test_fulfill_over_committed_preorder_rejected_and_nothing_posted(client, engine):
    """Server-side gate — a client fulfilling a pre-order sale with a
    purchase that doesn't actually cover it must be rejected, with the
    whole attempt (including the new purchase) rolled back.
    """
    sku = _post_new_item(client, "API Preorder Insufficient Widget", "fungible")
    preorder = client.post("/api/preorder-sales", json={"sku": sku, "quantity": 10}).json()

    body = {
        "preorder_sale_ids": [preorder["id"]],
        "purchase": {
            "purchase_date": "2026-09-14",
            "vendor_description": "Not enough stock",
            "total_amount_paid": 300000,
            "currency": "IDR",
            "shipping_mode": "none",
            "lines": [
                {
                    "sku": sku,
                    "quantity": 3,
                    "pricing_mode": "direct",
                    "price_entry_mode": "per_unit",
                    "price_value": 100000,
                    "ships_separately": False,
                }
            ],
        },
    }
    resp = client.post("/api/preorder-sales/fulfill", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "InsufficientStockError"

    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM preorder_sales WHERE id = :id"), {"id": preorder["id"]}).scalar_one()
        purchase_count = conn.execute(
            text("SELECT COUNT(*) FROM purchases WHERE vendor_description = 'Not enough stock'")
        ).scalar_one()
    assert status == "pending"
    assert purchase_count == 0


def test_fulfill_mismatched_items_rejected(client):
    sku_a = _post_new_item(client, "API Preorder Mismatch A", "fungible")
    sku_b = _post_new_item(client, "API Preorder Mismatch B", "fungible")
    preorder_a = client.post("/api/preorder-sales", json={"sku": sku_a, "quantity": 1}).json()
    preorder_b = client.post("/api/preorder-sales", json={"sku": sku_b, "quantity": 1}).json()

    body = {
        "preorder_sale_ids": [preorder_a["id"], preorder_b["id"]],
        "purchase": {
            "purchase_date": "2026-09-14",
            "vendor_description": "Mismatch attempt",
            "total_amount_paid": 100000,
            "currency": "IDR",
            "shipping_mode": "none",
            "lines": [
                {
                    "sku": sku_a,
                    "quantity": 1,
                    "pricing_mode": "direct",
                    "price_entry_mode": "per_unit",
                    "price_value": 100000,
                    "ships_separately": False,
                }
            ],
        },
    }
    resp = client.post("/api/preorder-sales/fulfill", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "PreorderItemMismatchError"


def test_preorder_sales_page_renders(client):
    resp = client.get("/preorders")
    assert resp.status_code == 200
    assert "Pre-Order Sales" in resp.text
    assert "preorder_sales.js" in resp.text


def test_items_search_can_be_restricted_to_fungible_only(client):
    fungible_sku = _post_new_item(client, "Search Fungible Widget", "fungible")
    serialized_sku = _post_new_item(client, "Search Serialized Widget", "serialized", category_code="WATCHES")

    results = client.get("/api/items/search?q=Search&identity=fungible").json()
    skus = {r["sku"] for r in results}
    assert fungible_sku in skus
    assert serialized_sku not in skus
