"""Consignment tracking reached through the real HTTP/JSON layer
(Milestone 8) — proves the web endpoints call the real inventory.consignment
engine (not a separate, weaker web-layer check), including the single
"Mark as Sold" entry point routing decision in webapp/api.py.
"""
from __future__ import annotations

from sqlalchemy import text


def _post_new_item(auth_client, name, identity_mode, category_code="OTHERS"):
    resp = auth_client.post(
        "/api/items", json={"name": name, "category_code": category_code, "identity_mode": identity_mode}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["sku"]


def _post_consignor(auth_client, name="API Test Consignor", contact_info="0812-0000-0000"):
    resp = auth_client.post("/api/consignors", json={"name": name, "contact_info": contact_info})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _post_intake(auth_client, consignor_id, name="API Consigned Watch", category_code="WATCHES", quantity=1):
    resp = auth_client.post(
        "/api/consignment/intake",
        json={
            "consignor_id": consignor_id,
            "new_item_name": name,
            "new_item_category_code": category_code,
            "quantity": quantity,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_list_consignors(auth_client):
    consignor = _post_consignor(auth_client, name="API Consignor One")
    assert consignor["name"] == "API Consignor One"

    listed = auth_client.get("/api/consignors").json()
    assert any(c["id"] == consignor["id"] for c in listed)


def test_create_consignor_rejects_blank_name(auth_client):
    resp = auth_client.post("/api/consignors", json={"name": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ValidationError"


def test_intake_creates_consigned_item_through_real_engine(auth_client, engine):
    consignor = _post_consignor(auth_client)
    result = _post_intake(auth_client, consignor["id"], quantity=2)

    assert result["sku"].startswith("CONSIGN-WATCH-")
    assert result["consignor_id"] == consignor["id"]
    assert len(result["serial_ids"]) == 2

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT identity_mode, consignor_id FROM items WHERE sku = :sku"), {"sku": result["sku"]}
        ).mappings().first()
    assert row["identity_mode"] == "serialized"
    assert row["consignor_id"] == consignor["id"]

    detail = auth_client.get(f"/api/items/{result['sku']}").json()
    assert detail["quantity"] == 2
    assert detail["cost_basis"] == "0"
    assert detail["consignor_id"] == consignor["id"]
    assert detail["consignor_name"] == consignor["name"]


def test_intake_reuses_existing_consigned_item(auth_client):
    consignor = _post_consignor(auth_client)
    first = _post_intake(auth_client, consignor["id"], quantity=1)

    resp = auth_client.post(
        "/api/consignment/intake", json={"consignor_id": consignor["id"], "sku": first["sku"], "quantity": 1}
    )
    assert resp.status_code == 201, resp.text
    second = resp.json()
    assert second["item_id"] == first["item_id"]
    assert not (set(first["serial_ids"]) & set(second["serial_ids"]))


def test_intake_rejects_nonexistent_consignor(auth_client):
    resp = auth_client.post(
        "/api/consignment/intake",
        json={"consignor_id": 999999, "new_item_name": "X", "new_item_category_code": "OTHERS", "quantity": 1},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ConsignorNotFoundError"


def test_intake_rejects_reusing_a_plain_owned_item(auth_client):
    consignor = _post_consignor(auth_client)
    sku = _post_new_item(auth_client, "Plain Owned Serialized Item", "serialized", category_code="WATCHES")
    resp = auth_client.post(
        "/api/consignment/intake", json={"consignor_id": consignor["id"], "sku": sku, "quantity": 1}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_type"] == "ItemNotConsignedError"


def test_mark_as_sold_on_consigned_unit_routes_to_consignment_engine(auth_client, engine):
    """The SAME endpoint used for an ordinary item's Mark-as-Sold
    (/api/items/{sku}/serial-units/{serial_id}/deplete) must detect (via
    the item's consignor_id) that this is a consigned unit and route
    internally to inventory.consignment.sell_consigned_unit — one
    consistent entry point, not two different "sell" buttons/endpoints.
    """
    consignor = _post_consignor(auth_client)
    intake = _post_intake(auth_client, consignor["id"], quantity=1)
    serial_id = intake["serial_ids"][0]

    resp = auth_client.post(
        f"/api/items/{intake['sku']}/serial-units/{serial_id}/deplete",
        json={"reference": "eBay #777"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # A ConsignmentSaleResult, not a bare SerialDepletionResult — proves the
    # consignment-specific orchestration actually ran (real depletion +
    # real reimbursement record), not just a plain depletion.
    assert body["status"] == "unpaid"
    assert body["consignor_id"] == consignor["id"]
    assert "reimbursement_id" in body
    assert body["depletion"]["acquired_cost"] == "0"

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, consignor_id FROM consignor_reimbursements WHERE id = :id"),
            {"id": body["reimbursement_id"]},
        ).mappings().first()
    assert row["status"] == "unpaid"
    assert row["consignor_id"] == consignor["id"]


def test_mark_as_sold_on_plain_owned_unit_still_uses_plain_depletion(auth_client):
    """The routing decision must not affect an ordinary, non-consigned
    serialized item — same endpoint, same plain depletion behavior as
    Milestone 5 shipped it.
    """
    sku = _post_new_item(auth_client, "Plain Owned Watch For Sale", "serialized", category_code="WATCHES")
    detail = auth_client.get(f"/api/items/{sku}").json()
    assert detail["quantity"] == 0  # no purchase yet — nothing to sell, proves this is a genuinely plain item

    # Buy one unit for real, then sell it — must NOT create a reimbursement.
    purchase_body = {
        "purchase_date": "2026-09-15",
        "vendor_description": "Owned stock purchase",
        "total_amount_paid": 500000,
        "currency": "IDR",
        "shipping_mode": "none",
        "lines": [
            {
                "sku": sku, "quantity": 1, "pricing_mode": "direct",
                "price_entry_mode": "total", "price_value": 500000, "ships_separately": False,
            }
        ],
    }
    purchase_resp = auth_client.post("/api/purchases", json=purchase_body)
    assert purchase_resp.status_code == 201, purchase_resp.text
    serial_id = purchase_resp.json()["lines"][0]["serial_ids"][0]

    resp = auth_client.post(
        f"/api/items/{sku}/serial-units/{serial_id}/deplete", json={"reference": "Plain sale"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # A bare SerialDepletionResult — no reimbursement fields at all.
    assert "status" not in body or body.get("status") != "unpaid"
    assert "reimbursement_id" not in body

    reimbursements = auth_client.get("/api/consignment/reimbursements").json()
    assert reimbursements == []


def test_double_sell_of_consigned_unit_rejected(auth_client):
    consignor = _post_consignor(auth_client)
    intake = _post_intake(auth_client, consignor["id"], quantity=1)
    serial_id = intake["serial_ids"][0]

    first = auth_client.post(f"/api/items/{intake['sku']}/serial-units/{serial_id}/deplete", json={})
    assert first.status_code == 200

    second = auth_client.post(f"/api/items/{intake['sku']}/serial-units/{serial_id}/deplete", json={})
    assert second.status_code == 400
    assert second.json()["detail"]["error_type"] == "AlreadyDepletedError"


def test_list_and_mark_paid_reimbursement_through_real_engine(auth_client, engine):
    consignor = _post_consignor(auth_client)
    intake = _post_intake(auth_client, consignor["id"], quantity=1)
    serial_id = intake["serial_ids"][0]
    sale = auth_client.post(
        f"/api/items/{intake['sku']}/serial-units/{serial_id}/deplete", json={}
    ).json()

    unpaid = auth_client.get("/api/consignment/reimbursements?status=unpaid").json()
    assert any(r["id"] == sale["reimbursement_id"] for r in unpaid)

    resp = auth_client.post(
        f"/api/consignment/reimbursements/{sale['reimbursement_id']}/mark-paid",
        json={"payment_reference": "Paid via BCA transfer"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    assert body["reference"] == "Paid via BCA transfer"

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM consignor_reimbursements WHERE id = :id"), {"id": sale["reimbursement_id"]}
        ).mappings().first()
    assert row["status"] == "paid"

    # Double mark-paid must fail cleanly — no reopen/reverse flow.
    resp2 = auth_client.post(f"/api/consignment/reimbursements/{sale['reimbursement_id']}/mark-paid", json={})
    assert resp2.status_code == 400
    assert resp2.json()["detail"]["error_type"] == "ReimbursementNotUnpaidError"


def test_reimbursements_filter_by_consignor(auth_client):
    consignor_a = _post_consignor(auth_client, name="Filter API Consignor A")
    consignor_b = _post_consignor(auth_client, name="Filter API Consignor B")
    intake_a = _post_intake(auth_client, consignor_a["id"], name="Filter API Item A")
    intake_b = _post_intake(auth_client, consignor_b["id"], name="Filter API Item B")
    auth_client.post(f"/api/items/{intake_a['sku']}/serial-units/{intake_a['serial_ids'][0]}/deplete", json={})
    auth_client.post(f"/api/items/{intake_b['sku']}/serial-units/{intake_b['serial_ids'][0]}/deplete", json={})

    only_a = auth_client.get(f"/api/consignment/reimbursements?consignor_id={consignor_a['id']}").json()
    assert len(only_a) == 1
    assert only_a[0]["consignor_id"] == consignor_a["id"]


def test_consignors_page_renders(auth_client):
    resp = auth_client.get("/consignors")
    assert resp.status_code == 200
    assert "Consignors" in resp.text
    assert "consignors.js" in resp.text


def test_consignment_intake_page_renders(auth_client):
    resp = auth_client.get("/consignment/intake")
    assert resp.status_code == 200
    assert "Consignment Intake" in resp.text
    assert "consignment_intake.js" in resp.text


def test_consignor_reimbursements_page_renders(auth_client):
    resp = auth_client.get("/consignment/reimbursements")
    assert resp.status_code == 200
    assert "Consignor Reimbursements" in resp.text
    assert "consignor_reimbursements.js" in resp.text
