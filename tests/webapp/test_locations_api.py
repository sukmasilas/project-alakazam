"""Locations screen + item-location wiring, reached through the JSON API —
shelving/location feature, confirmed 2026-10-01. Mirrors
tests/webapp/test_items_api.py's style: real engine through the real HTTP
layer, not a JS-only illusion.
"""
from __future__ import annotations


def test_create_and_list_location(client):
    resp = client.post("/api/locations", json={"name": "Shelf A-3"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Shelf A-3"

    listed = client.get("/api/locations").json()
    assert any(r["id"] == body["id"] and r["name"] == "Shelf A-3" for r in listed)


def test_create_location_duplicate_name_rejected(client):
    ok = client.post("/api/locations", json={"name": "Warehouse B"})
    assert ok.status_code == 201
    dupe = client.post("/api/locations", json={"name": "Warehouse B"})
    assert dupe.status_code == 400
    assert dupe.json()["detail"]["error_type"] == "DuplicateLocationNameError"


def test_create_item_with_location_persists_and_shows_on_detail(client):
    loc = client.post("/api/locations", json={"name": "Front Counter"}).json()
    item = client.post(
        "/api/items",
        json={"name": "Widget With Location", "category_code": "OTHERS", "identity_mode": "fungible", "location_id": loc["id"]},
    ).json()

    detail = client.get(f"/api/items/{item['sku']}").json()
    assert detail["location_id"] == loc["id"]
    assert detail["location_name"] == "Front Counter"


def test_create_item_with_no_location_is_nullable(client):
    item = client.post(
        "/api/items",
        json={"name": "Widget No Location", "category_code": "OTHERS", "identity_mode": "fungible"},
    ).json()
    detail = client.get(f"/api/items/{item['sku']}").json()
    assert detail["location_id"] is None
    assert detail["location_name"] is None


def test_change_item_location_from_item_detail(client):
    loc_a = client.post("/api/locations", json={"name": "Shelf 1"}).json()
    loc_b = client.post("/api/locations", json={"name": "Shelf 2"}).json()
    item = client.post(
        "/api/items",
        json={"name": "Movable Widget", "category_code": "OTHERS", "identity_mode": "fungible", "location_id": loc_a["id"]},
    ).json()

    changed = client.post(f"/api/items/{item['sku']}/location", json={"location_id": loc_b["id"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["location_id"] == loc_b["id"]

    detail = client.get(f"/api/items/{item['sku']}").json()
    assert detail["location_id"] == loc_b["id"]


def test_change_item_location_to_unknown_id_rejected(client):
    item = client.post(
        "/api/items",
        json={"name": "Widget Bad Loc Change", "category_code": "OTHERS", "identity_mode": "fungible"},
    ).json()
    resp = client.post(f"/api/items/{item['sku']}/location", json={"location_id": 999999})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ValidationError"


def test_opening_inventory_new_item_with_location(client):
    loc = client.post("/api/locations", json={"name": "Back Room"}).json()
    resp = client.post(
        "/api/opening-inventory",
        json={
            "quantity": 3,
            "price_entry_mode": "per_unit",
            "price_value": "10000",
            "new_item": {"name": "Opening Widget", "category_code": "OTHERS", "identity_mode": "fungible", "location_id": loc["id"]},
        },
    )
    assert resp.status_code == 201, resp.text
    sku = resp.json()["lines"][0]["sku"]
    detail = client.get(f"/api/items/{sku}").json()
    assert detail["location_id"] == loc["id"]


def test_consignment_intake_new_item_with_location(client):
    loc = client.post("/api/locations", json={"name": "Consignment Rack 1"}).json()
    consignor = client.post("/api/consignors", json={"name": "Budi Santoso"}).json()
    resp = client.post(
        "/api/consignment/intake",
        json={
            "consignor_id": consignor["id"],
            "new_item_name": "Consigned Rolex",
            "new_item_category_code": "WATCHES",
            "new_item_location_id": loc["id"],
            "quantity": 1,
        },
    )
    assert resp.status_code == 201, resp.text
    sku = resp.json()["sku"]
    detail = client.get(f"/api/items/{sku}").json()
    assert detail["location_id"] == loc["id"]
