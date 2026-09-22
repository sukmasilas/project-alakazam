"""Item listing/search/creation — real Milestone 2 engine reached through
the JSON API, not a JS-only illusion.
"""
from __future__ import annotations


def test_create_item_generates_real_sku(client):
    resp = client.post(
        "/api/items",
        json={"name": "Charizard VMAX Box", "category_code": "TCG", "identity_mode": "fungible"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sku"] == "TCG-CHARIZARD-VMAX-0001"
    assert body["name"] == "Charizard VMAX Box"


def test_create_item_sequence_scoped_to_prefix_slug(client):
    first = client.post(
        "/api/items", json={"name": "Charizard VMAX Box", "category_code": "TCG", "identity_mode": "fungible"}
    ).json()
    second = client.post(
        "/api/items", json={"name": "Charizard VMAX Deck", "category_code": "TCG", "identity_mode": "fungible"}
    ).json()
    assert first["sku"] == "TCG-CHARIZARD-VMAX-0001"
    assert second["sku"] == "TCG-CHARIZARD-VMAX-0002"


def test_create_item_duplicate_sku_rejected_by_real_engine(client):
    ok = client.post(
        "/api/items", json={"name": "Rolex Submariner", "category_code": "WATCHES", "identity_mode": "serialized", "sku": "WATCH-ROLEX-0001"}
    )
    assert ok.status_code == 201

    dupe = client.post(
        "/api/items", json={"name": "Another Rolex", "category_code": "WATCHES", "identity_mode": "serialized", "sku": "WATCH-ROLEX-0001"}
    )
    assert dupe.status_code == 400
    assert dupe.json()["detail"]["error_type"] == "DuplicateSkuError"


def test_sku_candidates_and_check_endpoints(client):
    candidates = client.get("/api/skus/candidates?category_code=TCG&name=Pikachu%20Promo").json()
    assert candidates["suggestion"] == "TCG-PIKACHU-PROMO-0001"

    check_before = client.get("/api/skus/check?sku=TCG-PIKACHU-PROMO-0001").json()
    assert check_before["exists"] is False

    client.post("/api/items", json={"name": "Pikachu Promo", "category_code": "TCG", "identity_mode": "fungible"})

    check_after = client.get("/api/skus/check?sku=TCG-PIKACHU-PROMO-0001").json()
    assert check_after["exists"] is True


def test_item_search_typeahead(client):
    client.post("/api/items", json={"name": "Omega Speedmaster", "category_code": "WATCHES", "identity_mode": "serialized"})
    client.post("/api/items", json={"name": "Brake Pad Set", "category_code": "AUTOMOTIVE", "identity_mode": "fungible"})

    results = client.get("/api/items/search?q=omega").json()
    assert len(results) == 1
    assert results[0]["sku"] == "WATCH-OMEGA-SPEEDMASTER-0001"

    empty = client.get("/api/items/search?q=").json()
    assert empty == []


def test_categories_listed_and_stats_start_empty(client):
    cats = client.get("/api/categories").json()
    codes = {c["code"] for c in cats}
    assert codes == {"TCG", "WATCHES", "AUTOMOTIVE", "TOYS_COLLECTIBLES", "OTHERS"}

    stats = client.get("/api/categories/stats").json()
    for row in stats:
        assert row["item_count"] == 0
        assert row["quantity"] == 0
        assert row["cost_basis"] == "0"


def test_item_detail_404_for_unknown_sku(client):
    resp = client.get("/api/items/DOES-NOT-EXIST-0001")
    assert resp.status_code == 404
