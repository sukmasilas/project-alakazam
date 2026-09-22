"""Basic rendering smoke tests for all four real screens against seeded
data — each page must return 200 and contain its expected shell markup
(the real data itself is fetched client-side via the JSON API, already
covered in test_items_api.py / test_purchases_api.py).
"""
from __future__ import annotations


def test_root_redirects_to_inventory(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inventory"


def test_inventory_page_renders(client):
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert "Inventory" in resp.text
    assert "inventory.js" in resp.text


def test_purchase_entry_page_renders(client):
    resp = client.get("/purchases/new")
    assert resp.status_code == 200
    assert "Purchase Entry" in resp.text
    assert "purchase_entry.js" in resp.text


def test_purchase_history_page_renders(client):
    resp = client.get("/purchases")
    assert resp.status_code == 200
    assert "Purchase History" in resp.text


def test_item_detail_page_renders_with_real_sku(client):
    created = client.post(
        "/api/items", json={"name": "Vintage Poster Lot", "category_code": "OTHERS", "identity_mode": "fungible"}
    ).json()
    resp = client.get(f"/items/{created['sku']}")
    assert resp.status_code == 200
    assert created["sku"] in resp.text


def test_health_check_is_reachable_with_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
