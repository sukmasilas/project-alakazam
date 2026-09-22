"""eBay sales import reached through the real HTTP/JSON layer (Milestone
6) — proves the upload endpoint really decodes/parses a CSV, and the
match/skip/process endpoints call the real ingestion.ebay_import engine
(not a separate, weaker web-layer check), same standard as
tests/webapp/test_depletions_api.py.
"""
from __future__ import annotations

import io

from sqlalchemy import text

_HEADER = (
    "Transaction creation date,Type,Order number,Legacy order ID,Buyer username,Buyer name,"
    "Ship to city,Ship to province/region/state,Ship to zip,Ship to country,Net amount,"
    "Payout currency,Payout date,Payout ID,Payout method,Payout status,Reason for hold,"
    "Item ID,Transaction ID,Item title,Custom label,Quantity,Item subtotal,Shipping and handling,"
    "Seller collected tax,eBay collected tax,Final Value Fee - fixed,Final Value Fee - variable,"
    "Regulatory operating fee,Very high fee,Below standard performance fee,International fee,"
    "Charity donation,Deposit processing fee,Gross transaction amount,Transaction currency,"
    "Exchange rate,Reference ID,Description\n"
)


def _csv_bytes(rows: list[str]) -> bytes:
    preamble = "\n".join([f"junk preamble {i}" for i in range(9)]) + "\nSeller,api.test.seller\n"
    text_body = preamble + _HEADER + "\n".join(rows) + "\n"
    return ("﻿" + text_body).encode("utf-8")  # real eBay exports carry a BOM


def _order_row(order_no, txn_id, title, qty=1):
    return (
        f'"Aug 15, 2026",Order,{order_no},{order_no},buyer,Buyer,City,ST,0,US,10,USD,'
        f"--,--,--,--,--,90000{order_no[-3:]},{txn_id},{title},--,{qty},"
        f"10,0,--,--,--,-1,--,--,--,-0.1,--,--,10,USD,--,--,--"
    )


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


def _upload(client, filename, rows):
    files = {"file": (filename, io.BytesIO(_csv_bytes(rows)), "text/csv")}
    return client.post("/api/ebay-import/upload", files=files)


class TestUploadEndpoint:
    def test_upload_parses_and_stores_real_csv_bytes(self, client, engine):
        resp = _upload(
            client,
            "aug.csv",
            [_order_row("11-00000-00001", "9000000001", "Widget A"), _order_row("11-00000-00002", "9000000002", "Widget B", qty=2)],
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["seller"] == "api.test.seller"
        assert body["order_rows_stored"] == 2

        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM ebay_sales_rows")).scalar_one()
        assert count == 2

    def test_reuploading_the_same_file_does_not_double_store(self, client):
        rows = [_order_row("11-00000-00001", "9000000001", "Widget A")]
        first = _upload(client, "aug.csv", rows)
        second = _upload(client, "aug.csv", rows)
        assert first.json()["order_rows_stored"] == 1
        assert second.json()["order_rows_stored"] == 0
        assert second.json()["order_rows_duplicate"] == 1

    def test_malformed_file_returns_a_clean_400_not_a_500(self, client):
        files = {"file": ("bad.csv", io.BytesIO(b"not,a,real,ebay,export\n1,2,3,4,5\n"), "text/csv")}
        resp = client.post("/api/ebay-import/upload", files=files)
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_type"] == "EbayCsvFormatError"


class TestReviewFlowThroughApi:
    def test_full_match_then_process_flow_for_a_fungible_row(self, client, engine):
        sku = _post_fungible_purchase(client, "Brake Pad Set", quantity=10, price_value=100_000)
        upload_resp = _upload(client, "aug.csv", [_order_row("11-00000-00001", "9000000001", "Brake Pad Set", qty=3)])
        batch_id = upload_resp.json()["batch_id"]

        rows = client.get(f"/api/ebay-import/rows?batch_id={batch_id}").json()
        assert len(rows) == 1
        row_id = rows[0]["id"]
        assert rows[0]["review_status"] == "pending"

        match_resp = client.post(f"/api/ebay-import/rows/{row_id}/match", json={"sku": sku})
        assert match_resp.status_code == 200, match_resp.text
        assert match_resp.json()["review_status"] == "matched"

        process_resp = client.post(f"/api/ebay-import/process?batch_id={batch_id}", json={})
        assert process_resp.status_code == 200, process_resp.text
        results = process_resp.json()
        assert len(results) == 1
        assert results[0]["success"] is True

        with engine.connect() as conn:
            qty = conn.execute(
                text(
                    "SELECT COALESCE(SUM(quantity), 0) FROM fungible_depletions fd "
                    "JOIN items i ON i.id = fd.item_id WHERE i.sku = :sku"
                ),
                {"sku": sku},
            ).scalar_one()
        assert qty == 3

        posted_row = client.get(f"/api/ebay-import/rows?batch_id={batch_id}").json()[0]
        assert posted_row["review_status"] == "posted"

    def test_skip_action_never_depletes_anything(self, client, engine):
        _post_fungible_purchase(client, "Oil Filter", quantity=5, price_value=50_000)
        upload_resp = _upload(client, "aug.csv", [_order_row("11-00000-00001", "9000000001", "Oil Filter")])
        batch_id = upload_resp.json()["batch_id"]
        row_id = client.get(f"/api/ebay-import/rows?batch_id={batch_id}").json()[0]["id"]

        skip_resp = client.post(f"/api/ebay-import/rows/{row_id}/skip")
        assert skip_resp.status_code == 200
        assert skip_resp.json()["review_status"] == "skipped"

        process_resp = client.post(f"/api/ebay-import/process?batch_id={batch_id}", json={})
        assert process_resp.json() == []

        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM fungible_depletions")).scalar_one()
        assert count == 0

    def test_match_endpoint_rejects_a_client_lying_about_serial_count(self, client):
        resp = client.post(
            "/api/purchases",
            json={
                "purchase_date": "2026-09-08",
                "vendor_description": "Stock-up: Rolex",
                "total_amount_paid": 30_000_000,
                "currency": "IDR",
                "shipping_mode": "none",
                "lines": [
                    {
                        "quantity": 3,
                        "pricing_mode": "direct",
                        "price_entry_mode": "total",
                        "price_value": 30_000_000,
                        "ships_separately": False,
                        "new_item": {"name": "Rolex Submariner", "category_code": "WATCHES", "identity_mode": "serialized"},
                    }
                ],
            },
        )
        sku = resp.json()["lines"][0]["sku"]
        serials = resp.json()["lines"][0]["serial_ids"]

        upload_resp = _upload(client, "aug.csv", [_order_row("11-00000-00001", "9000000001", "Rolex Submariner", qty=2)])
        batch_id = upload_resp.json()["batch_id"]
        row_id = client.get(f"/api/ebay-import/rows?batch_id={batch_id}").json()[0]["id"]

        # Client claims only 1 serial even though the row needs 2.
        resp = client.post(f"/api/ebay-import/rows/{row_id}/match", json={"sku": sku, "serial_ids": [serials[0]]})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_type"] == "InvalidRowActionError"

    def test_refund_rows_visible_but_not_actionable_through_the_api(self, client):
        refund_row = (
            '"Aug 16, 2026",Refund,11-00000-00099,11-00000-00099,b,B,City,ST,0,US,-10,USD,'
            "--,--,--,--,--,--,--,Refunded Widget,--,--,--,--,--,-1,0.4,2,--,--,--,0.2,--,--,-10,USD,--,Cancel,--"
        )
        upload_resp = _upload(client, "aug.csv", [refund_row])
        batch_id = upload_resp.json()["batch_id"]
        rows = client.get(f"/api/ebay-import/rows?batch_id={batch_id}").json()
        assert len(rows) == 1
        assert rows[0]["row_type"] == "Refund"

        skip_resp = client.post(f"/api/ebay-import/rows/{rows[0]['id']}/skip")
        assert skip_resp.status_code == 400
        assert skip_resp.json()["detail"]["error_type"] == "InvalidRowActionError"

    def test_ebay_import_page_renders(self, client):
        resp = client.get("/ebay-import")
        assert resp.status_code == 200
        assert "eBay Sales Import" in resp.text
        assert "ebay_import.js" in resp.text
