"""Tests for the reverse-proxy mount-prefix support (ASGI `root_path`).

See CLAUDE.md's "Real production deployment shape, confirmed 2026-09-25"
entry: in production, nginx proxies `dotworks.net/alakazam/*` to this app
(started with `uvicorn ... --root-path /alakazam`), stripping the
`/alakazam` prefix off the request line before it reaches uvicorn. Real
uvicorn then reconstructs `scope["path"]` as `root_path + <the path it
actually received>` (verified directly against uvicorn's
`httptools_impl.py` before writing these tests) — so `scope["path"]`
*always* carries the root_path prefix inside a real running app, and
Starlette's routing strips it back off internally before matching routes.

Starlette's `TestClient` does **not** perform that root_path-prefixing
concatenation itself — it sets `scope["path"]` to exactly what's passed to
`.get()`/`.post()`. So the faithful way to unit-test root_path handling
here is the opposite of how you'd curl a real running server: pass the
ALREADY-PREFIXED path (see the `prefixed_client` fixture in conftest.py for
the full reasoning, confirmed empirically, not just via reading source).
This file's manual/real end-to-end verification (see the task report) used
the real curl-the-unprefixed-path convention instead, against an actual
`uvicorn --root-path /alakazam` process, to cross-check this test suite's
own faithfulness.

The plain `client` fixture (root_path="") used throughout the rest of the
test suite already proves the unprefixed/local-dev case is an exact no-op —
this file only adds the prefixed case.
"""
from __future__ import annotations

PREFIX = "/alakazam"


def test_root_redirect_is_prefixed(prefixed_client):
    resp = prefixed_client.get(f"{PREFIX}/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{PREFIX}/inventory"


def test_root_redirect_is_not_prefixed_without_root_path(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inventory"


def test_inventory_page_links_are_prefixed(prefixed_client):
    resp = prefixed_client.get(f"{PREFIX}/inventory")
    assert resp.status_code == 200
    # JS-facing base path global, consumed by common.js's appUrl() helper.
    assert 'window.APP_ROOT_PATH = "/alakazam";' in resp.text
    # Static assets.
    assert "/alakazam/static/css/app.css" in resp.text
    assert "/alakazam/static/js/common.js" in resp.text
    assert "/alakazam/static/js/calc.js" in resp.text
    assert "/alakazam/static/js/inventory.js" in resp.text
    # Nav links.
    assert 'href="/alakazam/inventory"' in resp.text
    assert 'href="/alakazam/purchases/new"' in resp.text
    assert 'href="/alakazam/purchases"' in resp.text
    assert 'href="/alakazam/sales"' in resp.text
    assert 'href="/alakazam/ebay-import"' in resp.text
    assert 'href="/alakazam/preorders"' in resp.text
    assert 'href="/alakazam/consignors"' in resp.text
    assert 'href="/alakazam/consignment/intake"' in resp.text
    assert 'href="/alakazam/consignment/reimbursements"' in resp.text
    # Never double-prefixed and never a bare unprefixed leftover.
    assert "/alakazam/alakazam" not in resp.text
    assert 'href="/inventory"' not in resp.text
    assert 'src="/static' not in resp.text


def test_inventory_page_links_are_not_prefixed_without_root_path(client):
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert 'window.APP_ROOT_PATH = "";' in resp.text
    assert 'href="/static/css/app.css"' in resp.text
    assert 'src="/static/js/common.js"' in resp.text
    assert 'href="/inventory"' in resp.text
    assert 'href="/purchases/new"' in resp.text
    assert "/alakazam" not in resp.text


def test_item_detail_page_back_link_is_prefixed(prefixed_client):
    created = prefixed_client.post(
        f"{PREFIX}/api/items",
        json={"name": "Prefix Test Item", "category_code": "OTHERS", "identity_mode": "fungible"},
    ).json()
    resp = prefixed_client.get(f"{PREFIX}/items/{created['sku']}")
    assert resp.status_code == 200
    assert 'href="/alakazam/inventory"' in resp.text
    assert 'src="/alakazam/static/js/item_detail.js"' in resp.text


def test_static_asset_resolves_through_the_mount_when_prefixed(prefixed_client):
    # Exercises the nested StaticFiles Mount specifically — the one place
    # this codebase found where root_path must be threaded correctly
    # through Starlette's own internal path-stripping (Mount.matches ->
    # StaticFiles.get_path -> get_route_path), not just read once at the
    # top level. See the fixture docstring in conftest.py for why the
    # request path must include the prefix here.
    resp = prefixed_client.get(f"{PREFIX}/static/js/common.js")
    assert resp.status_code == 200
    assert "appUrl" in resp.text


def test_static_asset_still_resolves_without_root_path(client):
    resp = client.get("/static/js/common.js")
    assert resp.status_code == 200
    assert "appUrl" in resp.text


def test_health_check_unaffected_by_root_path(prefixed_client):
    resp = prefixed_client.get(f"{PREFIX}/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
