"""The login gate must actually block unauthenticated access — to page
routes (redirect to /login) and API routes (401 JSON) alike — and a
correct login must be required before either opens up.
"""
from __future__ import annotations

from tests.webapp.conftest import TEST_PASSWORD, TEST_USERNAME


def test_unauthenticated_page_redirects_to_login(client):
    resp = client.get("/inventory", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_unauthenticated_api_returns_401(client):
    resp = client.get("/api/items")
    assert resp.status_code == 401


def test_login_page_itself_is_public(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_wrong_password_rejected_and_no_session_granted(client):
    resp = client.post(
        "/login", data={"username": TEST_USERNAME, "password": "wrong-password", "next": "/inventory"}
    )
    assert resp.status_code == 401
    # No session was granted — a subsequent page request is still blocked.
    resp2 = client.get("/inventory", follow_redirects=False)
    assert resp2.status_code == 303


def test_wrong_username_rejected(client):
    resp = client.post(
        "/login", data={"username": "not-the-real-user", "password": TEST_PASSWORD, "next": "/inventory"}
    )
    assert resp.status_code == 401


def test_correct_login_grants_access_to_pages_and_api(auth_client):
    page_resp = auth_client.get("/inventory")
    assert page_resp.status_code == 200
    api_resp = auth_client.get("/api/items")
    assert api_resp.status_code == 200
    assert api_resp.json() == []


def test_logout_revokes_access(auth_client):
    assert auth_client.get("/inventory").status_code == 200
    logout_resp = auth_client.post("/logout", follow_redirects=False)
    assert logout_resp.status_code == 303
    resp = auth_client.get("/inventory", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_health_check_is_public(client):
    assert client.get("/health").status_code == 200
