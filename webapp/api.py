"""JSON API — the real backend behind every dynamic interaction on the four
screens (typeahead search, SKU/serial candidate generation, uniqueness
checks, purchase save, purchase-history detail). Every endpoint here calls
straight into ``inventory/*.py`` — no allocation math, SKU/serial
generation, or uniqueness logic is reimplemented at this layer.

Money/date serialization: every ``Decimal`` in a response is rendered as a
string (a whole-rupiah integer, e.g. ``"150000"``) rather than a JSON
number, so the client never round-trips currency through float — the same
"never float" discipline CLAUDE.md requires of the engine itself, carried
through to the wire format.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.engine import Connection

from inventory import depletions as depletions_engine
from inventory import items as items_engine
from inventory import purchases as purchases_engine
from inventory import queries
from inventory import serials as serials_engine
from inventory import sku as sku_engine
from inventory import uniqueness
from inventory.exceptions import AlakazamError
from webapp.dbdep import get_read_conn, get_write_conn
from webapp.schemas import (
    FungibleDepletionIn,
    NewItemCreateIn,
    PurchaseIn,
    SerialDepletionIn,
    to_purchase_input,
)

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------- #
# JSON-safe serialization for dataclasses coming out of inventory/*.py
# --------------------------------------------------------------------- #


def _convert(value):
    # This check must live HERE, not only in serialize()'s outer entry
    # point — a hand-built dict/list can contain nested dataclass
    # instances (e.g. api_item_detail()'s payload embeds a raw list of
    # FungiblePurchaseRow/SerializedUnitRow dataclasses), and every level
    # of recursion goes through _convert(), never back through serialize().
    # Real bug found by QA 2026-09-10: without this, a nested dataclass's
    # Decimal fields skipped this module's string-conversion entirely and
    # were left for FastAPI's default encoder to silently turn into JSON
    # floats — exactly the "never float" violation this module's docstring
    # exists to prevent.
    if is_dataclass(value) and not isinstance(value, type):
        return _convert(asdict(value))
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v) for v in value]
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, date_type):
        return value.isoformat()
    return value


def serialize(obj):
    return _convert(obj)


def _error_response(exc: AlakazamError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error_type": type(exc).__name__, "message": str(exc)},
    )


# --------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------- #


@router.get("/categories")
def api_list_categories(conn: Connection = Depends(get_read_conn)):
    cats = queries.list_categories(conn)
    return serialize(cats)


@router.get("/categories/stats")
def api_category_stats(conn: Connection = Depends(get_read_conn)):
    stats = queries.get_category_stats(conn)
    return serialize(stats)


# --------------------------------------------------------------------- #
# Items — Inventory screen, typeahead search, Item Detail
# --------------------------------------------------------------------- #


@router.get("/items")
def api_list_items(
    category: Optional[str] = None,
    identity: Optional[str] = None,
    q: Optional[str] = None,
    conn: Connection = Depends(get_read_conn),
):
    identity_mode = identity if identity in ("fungible", "serialized") else None
    rows = queries.list_items(conn, category_code=category, identity_mode=identity_mode, search=q)
    return serialize(rows)


@router.get("/items/search")
def api_search_items(q: str = "", limit: int = 8, conn: Connection = Depends(get_read_conn)):
    if not q.strip():
        return []
    return serialize(queries.search_items(conn, q, limit=limit))


@router.get("/items/{sku}")
def api_item_detail(sku: str, conn: Connection = Depends(get_read_conn)):
    item = queries.get_item_by_sku(conn, sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No item with that SKU.")
    stats = queries.get_item_stats(conn, sku)
    category = None
    for c in queries.list_categories(conn):
        if c.id == item.category_id:
            category = c
            break

    payload = {
        "sku": item.sku,
        "name": item.name,
        "identity_mode": item.identity_mode,
        "category_code": category.code if category else None,
        "category_name": category.name if category else None,
        "quantity": stats.quantity,
        "cost_basis": stats.cost_basis,
    }
    if item.identity_mode == "serialized":
        payload["serialized_units"] = queries.get_serialized_unit_rows(conn, item.id)
    else:
        payload["fungible_rows"] = queries.get_fungible_purchase_rows(conn, item.id)
    return serialize(payload)


@router.post("/items", status_code=201)
def api_create_item(body: NewItemCreateIn, conn: Connection = Depends(get_write_conn)):
    try:
        item = items_engine.create_item(
            conn, name=body.name, category_code=body.category_code,
            identity_mode=body.identity_mode, sku=body.sku,
        )
    except AlakazamError as exc:
        raise _error_response(exc) from exc
    return serialize(item)


# --------------------------------------------------------------------- #
# SKU / serial generation previews + real uniqueness checks. Generation
# here is a PREVIEW only (client-side JS also mirrors the pure
# slugify/sequence algorithm for live UI feedback, per the design doc) —
# the real, authoritative gate is always inventory.purchases.save_purchase()
# / inventory.items.create_item(), which re-derive and re-check everything
# server-side regardless of what the client displayed.
# --------------------------------------------------------------------- #


@router.get("/skus/candidates")
def api_sku_candidates(category_code: str, name: str, conn: Connection = Depends(get_read_conn)):
    category = queries.get_category_by_code(conn, category_code)
    if category is None:
        raise HTTPException(status_code=400, detail=f"Unknown category_code: {category_code!r}")
    slug = sku_engine.slugify_name(name)
    base = f"{category.sku_prefix}-{slug}"
    existing = queries.get_skus_for_prefix_slug(conn, base)
    suggestion = sku_engine.generate_sku(category.sku_prefix, name, existing)
    return {"sku_prefix": category.sku_prefix, "slug": slug, "base": base, "existing": existing, "suggestion": suggestion}


@router.get("/skus/check")
def api_sku_check(sku: str, conn: Connection = Depends(get_read_conn)):
    return {"exists": uniqueness.sku_exists(conn, sku)}


@router.get("/items/{sku}/serial-count")
def api_serial_count(sku: str, conn: Connection = Depends(get_read_conn)):
    item = queries.get_item_by_sku(conn, sku)
    if item is None:
        return {"existing_count": 0}
    return {"existing_count": queries.count_serial_units_for_item(conn, item.id)}


@router.get("/serials/candidates")
def api_serial_candidates(
    sku: str, quantity: int = 1, conn: Connection = Depends(get_read_conn)
):
    item = queries.get_item_by_sku(conn, sku)
    existing_count = queries.count_serial_units_for_item(conn, item.id) if item else 0
    candidates = serials_engine.generate_serials_for_line(sku, max(1, quantity), existing_count)
    return {"existing_count": existing_count, "candidates": candidates}


@router.get("/serials/check")
def api_serial_check(serial_id: str, conn: Connection = Depends(get_read_conn)):
    return {"exists": uniqueness.serial_exists(conn, serial_id)}


# --------------------------------------------------------------------- #
# Purchases — Purchase Entry save, Purchase History
# --------------------------------------------------------------------- #


@router.get("/purchases")
def api_list_purchases(conn: Connection = Depends(get_read_conn)):
    return serialize(queries.list_purchases(conn))


@router.get("/purchases/{purchase_ref}")
def api_purchase_detail(purchase_ref: str, conn: Connection = Depends(get_read_conn)):
    detail = queries.get_purchase_detail(conn, purchase_ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="No purchase with that reference.")
    return serialize(detail)


@router.post("/purchases", status_code=201)
def api_save_purchase(body: PurchaseIn, conn: Connection = Depends(get_write_conn)):
    purchase_input = to_purchase_input(body)
    try:
        saved = purchases_engine.save_purchase(conn, purchase_input)
    except AlakazamError as exc:
        raise _error_response(exc) from exc
    return serialize(saved)


# --------------------------------------------------------------------- #
# Milestone 5 — sale-side depletion. Every endpoint here calls straight
# into inventory.depletions — the real negative-stock guard (pre-check +
# DB trigger / atomic UPDATE, see that module's docstring) is never
# reimplemented at this layer. The server is always authoritative: a
# client-supplied quantity/serial_id is never trusted beyond what these
# real engine functions actually accept.
# --------------------------------------------------------------------- #


@router.post("/items/{sku}/deplete")
def api_deplete_fungible(sku: str, body: FungibleDepletionIn, conn: Connection = Depends(get_write_conn)):
    try:
        result = depletions_engine.deplete_fungible(
            conn,
            sku=sku,
            quantity=body.quantity,
            depletion_date=body.depletion_date,
            reference=body.reference,
        )
    except AlakazamError as exc:
        raise _error_response(exc) from exc
    return serialize(result)


@router.post("/items/{sku}/serial-units/{serial_id}/deplete")
def api_deplete_serial_unit(
    sku: str, serial_id: str, body: SerialDepletionIn, conn: Connection = Depends(get_write_conn)
):
    try:
        result = depletions_engine.deplete_serial_unit(
            conn,
            serial_id=serial_id,
            expected_sku=sku,
            sold_date=body.sold_date,
            reference=body.reference,
        )
    except AlakazamError as exc:
        raise _error_response(exc) from exc
    return serialize(result)


@router.get("/depletions")
def api_list_depletions(sku: Optional[str] = None, conn: Connection = Depends(get_read_conn)):
    return serialize(queries.list_depletions(conn, sku=sku))
