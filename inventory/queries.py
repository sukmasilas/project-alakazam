"""Read-side helpers: resolving categories/items, and the on-hand
quantity/cost rollups described in CLAUDE.md's brief ("On-hand quantity/cost
computation").

Milestone 5 update: on-hand quantity/cost is no longer acquisition-only.
``get_item_stats`` now nets total purchased against total depleted (see
``inventory/depletions.py``) for BOTH identity modes — a fungible item's
on-hand figures subtract ``fungible_depletions``, a serialized item's
on-hand figures only count ``serial_units`` still ``status = 'on_hand'``.
Every caller of ``get_item_stats``/``get_category_stats`` (the Inventory
table, Item Detail, category rollups) picks this up for free, with no
caller-side changes needed, since they never computed on-hand figures
themselves — they always deferred to this module. The one place that
deliberately does NOT reflect depletion is ``get_fungible_purchase_rows`` /
``get_purchase_detail`` (Purchase History, and Item Detail's per-purchase
breakdown) — a purchase's own recorded line figures are historical fact and
must never change because of a later, separate depletion event (see
CLAUDE.md's brief and ``docs/design/milestone-5-depletion-design.md``).

Milestone 3 (web app) note: everything below ``get_category_stats`` (the
original Milestone 2 boundary) is a Milestone-3 addition — plain read-only
SQL surfacing rows the schema already has (item listings, purchase-history
rows, purchase-ledger rows) for the real screens to render. None of it
computes money — allocation math stays exclusively in
``inventory/allocation.py`` / ``inventory/purchases.py``; these functions
only ever select and shape already-posted, already-computed columns.
``list_depletions`` (Milestone 5) follows the same rule — it reads
``fungible_depletions``/``serial_units`` verbatim, the real cost figures
were already computed once, at depletion time, by
``inventory/depletions.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass
class Category:
    id: int
    code: str
    name: str
    sku_prefix: str


@dataclass
class Item:
    id: int
    sku: str
    name: str
    category_id: int
    identity_mode: str


def get_category_by_code(conn: Connection, code: str) -> Optional[Category]:
    row = conn.execute(
        text("SELECT id, code, name, sku_prefix FROM categories WHERE code = :code"),
        {"code": code},
    ).mappings().first()
    return Category(**row) if row else None


def get_item_by_sku(conn: Connection, sku: str) -> Optional[Item]:
    row = conn.execute(
        text(
            "SELECT id, sku, name, category_id, identity_mode FROM items "
            "WHERE UPPER(sku) = UPPER(:sku)"
        ),
        {"sku": sku},
    ).mappings().first()
    return Item(**row) if row else None


def get_skus_for_prefix_slug(conn: Connection, base: str) -> list[str]:
    """Every existing item SKU that could plausibly match the
    ``{base}-####`` pattern for a given category-prefix + name-slug
    combination (inventory.sku.generate_sku scopes its sequence to exactly
    this). A cheap prefix filter in SQL; the exact regex match happens in
    Python (inventory.sku), same division of labor as the mockup (a single
    JS regex, here split across a narrowing SQL LIKE + a Python re.match).
    """
    rows = conn.execute(
        text("SELECT sku FROM items WHERE sku ILIKE :pattern"),
        {"pattern": f"{base}-%"},
    )
    return [row[0] for row in rows]


def count_serial_units_for_item(conn: Connection, item_id: int) -> int:
    row = conn.execute(
        text("SELECT COUNT(*) FROM serial_units WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).first()
    return int(row[0])


@dataclass
class ItemStats:
    item: Item
    quantity: int
    cost_basis: Decimal


def get_item_stats(conn: Connection, sku: str) -> ItemStats:
    """Current ON-HAND quantity and cost basis (Milestone 5: purchased
    minus depleted, not just purchased — see module docstring).

    For a fungible item: total purchased quantity/cost (item cost +
    shipping share, summed across every purchase line for this item) minus
    total depleted quantity/cost (``fungible_depletions`` — see
    ``inventory/depletions.py``). For a serialized item: count and
    cost-sum over only the serial units still ``status = 'on_hand'`` (a
    ``status = 'sold'`` unit's own ``acquired_cost`` is simply excluded,
    not subtracted after the fact — same end result, simpler query).
    """
    item = get_item_by_sku(conn, sku)
    if item is None:
        raise ValueError(f"No item with SKU {sku!r}")

    if item.identity_mode == "serialized":
        row = conn.execute(
            text(
                "SELECT COUNT(*), COALESCE(SUM(acquired_cost), 0) "
                "FROM serial_units WHERE item_id = :item_id AND status = 'on_hand'"
            ),
            {"item_id": item.id},
        ).first()
        return ItemStats(item=item, quantity=int(row[0]), cost_basis=Decimal(row[1]))

    purchased_row = conn.execute(
        text(
            "SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(line_total), 0) "
            "FROM purchase_line_items WHERE item_id = :item_id"
        ),
        {"item_id": item.id},
    ).first()
    depleted_row = conn.execute(
        text(
            "SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(total_cost), 0) "
            "FROM fungible_depletions WHERE item_id = :item_id"
        ),
        {"item_id": item.id},
    ).first()
    quantity = int(purchased_row[0]) - int(depleted_row[0])
    cost_basis = Decimal(purchased_row[1]) - Decimal(depleted_row[1])
    return ItemStats(item=item, quantity=quantity, cost_basis=cost_basis)


@dataclass
class CategoryStats:
    category: Category
    item_count: int
    quantity: int
    cost_basis: Decimal


def get_category_stats(conn: Connection) -> list[CategoryStats]:
    """One rollup per category: SKU count, total on-hand quantity, total
    cost basis — mirrors the Inventory screen's category rollup cards.
    Computed per-item via get_item_stats and summed, rather than a single
    denormalized SQL aggregate, so fungible and serialized items' different
    quantity/cost sources (purchase_line_items vs. serial_units) stay
    correct without a fragile UNION query.
    """
    categories = conn.execute(
        text("SELECT id, code, name, sku_prefix FROM categories ORDER BY id")
    ).mappings().all()

    results = []
    for cat_row in categories:
        category = Category(**cat_row)
        items = conn.execute(
            text("SELECT sku FROM items WHERE category_id = :category_id"),
            {"category_id": category.id},
        ).all()
        item_count = len(items)
        quantity = 0
        cost_basis = Decimal(0)
        for (sku,) in items:
            stats = get_item_stats(conn, sku)
            quantity += stats.quantity
            cost_basis += stats.cost_basis
        results.append(
            CategoryStats(
                category=category, item_count=item_count, quantity=quantity, cost_basis=cost_basis
            )
        )
    return results


# --------------------------------------------------------------------- #
# Milestone 3 (web app) additions — plain reads for the four real screens.
# See module docstring: no money computation happens here.
# --------------------------------------------------------------------- #


def list_categories(conn: Connection) -> list[Category]:
    rows = conn.execute(
        text("SELECT id, code, name, sku_prefix FROM categories ORDER BY name")
    ).mappings().all()
    return [Category(**row) for row in rows]


@dataclass
class ItemSummary:
    """One row for the Inventory table — an item plus its on-hand
    quantity/cost-basis (via ``get_item_stats``) and its category's display
    fields, for rendering without a second round trip per row.
    """

    sku: str
    name: str
    category_code: str
    category_name: str
    identity_mode: str
    quantity: int
    cost_basis: Decimal


def list_items(
    conn: Connection,
    category_code: Optional[str] = None,
    identity_mode: Optional[str] = None,
    search: Optional[str] = None,
) -> list[ItemSummary]:
    """Every item matching the given filters, each with its real on-hand
    stats. Mirrors the Inventory screen's category-rail + search + identity
    filter combination (ui-ux-design.md Screen 1).
    """
    query = """
        SELECT i.sku, i.name, i.identity_mode, c.code AS category_code, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        WHERE (:category_code IS NULL OR c.code = :category_code)
          AND (:identity_mode IS NULL OR i.identity_mode = :identity_mode)
          AND (
              :search IS NULL
              OR i.sku ILIKE :search_pat
              OR i.name ILIKE :search_pat
          )
        ORDER BY i.id
    """
    search_pat = f"%{search}%" if search else None
    rows = conn.execute(
        text(query),
        {
            "category_code": category_code,
            "identity_mode": identity_mode,
            "search": search,
            "search_pat": search_pat,
        },
    ).mappings().all()

    results = []
    for row in rows:
        stats = get_item_stats(conn, row["sku"])
        results.append(
            ItemSummary(
                sku=row["sku"],
                name=row["name"],
                category_code=row["category_code"],
                category_name=row["category_name"],
                identity_mode=row["identity_mode"],
                quantity=stats.quantity,
                cost_basis=stats.cost_basis,
            )
        )
    return results


@dataclass
class ItemSearchResult:
    sku: str
    name: str
    category_code: str
    category_name: str
    identity_mode: str


def search_items(
    conn: Connection, query: str, limit: int = 8, identity_mode: Optional[str] = None
) -> list[ItemSearchResult]:
    """Typeahead search for Purchase Entry's SKU combobox — any substring
    of SKU or item name, newest-created last (id order), capped at
    ``limit`` (design doc: "max 8 shown"). ``identity_mode`` (Milestone 7
    addition — optional, defaults to no filter, so every pre-existing
    caller is unaffected) narrows results to just ``'fungible'`` or
    ``'serialized'`` items — used by the Pre-Order Sales screen's item
    picker, which must only ever offer fungible items (see
    ``inventory/preorders.py``'s standing fungible-only rule).
    """
    pattern = f"%{query}%"
    rows = conn.execute(
        text(
            """
            SELECT i.sku, i.name, i.identity_mode, c.code AS category_code, c.name AS category_name
            FROM items i
            JOIN categories c ON c.id = i.category_id
            WHERE (i.sku ILIKE :pattern OR i.name ILIKE :pattern)
              AND (:identity_mode IS NULL OR i.identity_mode = :identity_mode)
            ORDER BY i.id
            LIMIT :limit
            """
        ),
        {"pattern": pattern, "limit": limit, "identity_mode": identity_mode},
    ).mappings().all()
    return [ItemSearchResult(**row) for row in rows]


@dataclass
class FungiblePurchaseRow:
    purchase_date: date_type
    purchase_ref: str
    quantity: int
    allocated_unit_cost: Decimal
    line_total: Decimal


def get_fungible_purchase_rows(conn: Connection, item_id: int) -> list[FungiblePurchaseRow]:
    rows = conn.execute(
        text(
            """
            SELECT p.purchase_date, p.purchase_ref, pli.quantity, pli.line_total
            FROM purchase_line_items pli
            JOIN purchases p ON p.id = pli.purchase_id
            WHERE pli.item_id = :item_id
            ORDER BY p.purchase_date, p.id
            """
        ),
        {"item_id": item_id},
    ).all()
    return [
        FungiblePurchaseRow(
            purchase_date=r.purchase_date,
            purchase_ref=r.purchase_ref,
            quantity=r.quantity,
            allocated_unit_cost=(Decimal(r.line_total) / r.quantity) if r.quantity else Decimal(0),
            line_total=Decimal(r.line_total),
        )
        for r in rows
    ]


@dataclass
class SerializedUnitRow:
    serial_id: str
    acquired_date: date_type
    cost: Decimal
    purchase_ref: str
    photo_reference: Optional[str]
    # Milestone 5 additions — default-valued so get_purchase_detail's own
    # construction of this same dataclass (which deliberately always
    # represents the unit as of its PURCHASE, not its current status; see
    # module docstring) doesn't need updating.
    status: str = "on_hand"
    sold_date: Optional[date_type] = None
    sold_reference: Optional[str] = None


def get_serialized_unit_rows(conn: Connection, item_id: int) -> list[SerializedUnitRow]:
    """Every unit ever purchased for this item — BOTH on_hand and sold —
    so Item Detail can show full traceability and gate its "Mark as Sold"
    action per row on each unit's own current status. This intentionally
    differs from get_item_stats' on-hand-only rollup above; the two serve
    different purposes (a live total vs. a full per-unit history).
    """
    rows = conn.execute(
        text(
            """
            SELECT su.serial_id, su.acquired_cost, su.photo_reference,
                   su.status, su.sold_date, su.sold_reference,
                   p.purchase_date, p.purchase_ref
            FROM serial_units su
            JOIN purchase_line_items pli ON pli.id = su.purchase_line_item_id
            JOIN purchases p ON p.id = pli.purchase_id
            WHERE su.item_id = :item_id
            ORDER BY p.purchase_date, su.id
            """
        ),
        {"item_id": item_id},
    ).all()
    return [
        SerializedUnitRow(
            serial_id=r.serial_id,
            acquired_date=r.purchase_date,
            cost=Decimal(r.acquired_cost),
            purchase_ref=r.purchase_ref,
            photo_reference=r.photo_reference,
            status=r.status,
            sold_date=r.sold_date,
            sold_reference=r.sold_reference,
        )
        for r in rows
    ]


@dataclass
class PurchaseSummary:
    id: int
    purchase_ref: str
    purchase_date: date_type
    vendor_description: str
    total_amount_paid: Decimal
    currency: str
    shipping_mode: str
    pooled_shipping_total: Optional[Decimal]
    pooled_shipping_method: Optional[str]
    lump_sum_active: bool
    lump_sum_total: Optional[Decimal]
    lump_sum_method: Optional[str]
    invoice_document_ref: Optional[str]
    invoice_ocr_status: Optional[str]
    invoice_parsed_fields: Optional[dict]
    line_count: int


def list_purchases(conn: Connection) -> list[PurchaseSummary]:
    """Every purchase header plus its line count, newest first — the
    Purchase History screen's read-only ledger (ui-ux-design.md Screen 4).
    """
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.purchase_ref, p.purchase_date, p.vendor_description,
                   p.total_amount_paid, p.currency, p.shipping_mode,
                   p.pooled_shipping_total, p.pooled_shipping_method,
                   p.lump_sum_active, p.lump_sum_total, p.lump_sum_method,
                   p.invoice_document_ref, p.invoice_ocr_status, p.invoice_parsed_fields,
                   COUNT(pli.id) AS line_count
            FROM purchases p
            LEFT JOIN purchase_line_items pli ON pli.purchase_id = p.id
            GROUP BY p.id
            ORDER BY p.purchase_date DESC, p.id DESC
            """
        )
    ).mappings().all()
    return [PurchaseSummary(**row) for row in rows]


@dataclass
class PurchaseLineDetail:
    id: int
    sku: str
    item_name: str
    quantity: int
    pricing_mode: str
    price_entry_mode: Optional[str]
    price_value: Optional[Decimal]
    lumpsum_weight_kg: Optional[Decimal]
    lumpsum_value: Optional[Decimal]
    ships_separately: bool
    manual_shipping_amount: Optional[Decimal]
    shipping_weight_kg: Optional[Decimal]
    allocated_item_cost: Decimal
    shipping_share: Decimal
    line_total: Decimal
    identity_mode: str
    serial_units: list[SerializedUnitRow] = field(default_factory=list)


@dataclass
class PurchaseDetail(PurchaseSummary):
    lines: list[PurchaseLineDetail] = field(default_factory=list)


def get_purchase_detail(conn: Connection, purchase_ref: str) -> Optional[PurchaseDetail]:
    """Full line-level detail for one purchase, for Purchase History's
    expand-to-accordion interaction and for Item Detail's "jump to source
    purchase" link.
    """
    header = conn.execute(
        text(
            """
            SELECT p.id, p.purchase_ref, p.purchase_date, p.vendor_description,
                   p.total_amount_paid, p.currency, p.shipping_mode,
                   p.pooled_shipping_total, p.pooled_shipping_method,
                   p.lump_sum_active, p.lump_sum_total, p.lump_sum_method,
                   p.invoice_document_ref, p.invoice_ocr_status, p.invoice_parsed_fields
            FROM purchases p WHERE p.purchase_ref = :ref
            """
        ),
        {"ref": purchase_ref},
    ).mappings().first()
    if header is None:
        return None

    line_rows = conn.execute(
        text(
            """
            SELECT pli.id, i.sku, i.name AS item_name, i.identity_mode,
                   pli.quantity, pli.pricing_mode, pli.price_entry_mode, pli.price_value,
                   pli.lumpsum_weight_kg, pli.lumpsum_value, pli.ships_separately,
                   pli.manual_shipping_amount, pli.shipping_weight_kg,
                   pli.allocated_item_cost, pli.shipping_share, pli.line_total
            FROM purchase_line_items pli
            JOIN items i ON i.id = pli.item_id
            WHERE pli.purchase_id = :purchase_id
            ORDER BY pli.id
            """
        ),
        {"purchase_id": header["id"]},
    ).mappings().all()

    lines = []
    for lr in line_rows:
        serial_units: list[SerializedUnitRow] = []
        if lr["identity_mode"] == "serialized":
            su_rows = conn.execute(
                text(
                    """
                    SELECT serial_id, acquired_cost, photo_reference, status, sold_date, sold_reference
                    FROM serial_units WHERE purchase_line_item_id = :line_id ORDER BY id
                    """
                ),
                {"line_id": lr["id"]},
            ).mappings().all()
            serial_units = [
                SerializedUnitRow(
                    serial_id=su["serial_id"],
                    acquired_date=header["purchase_date"],
                    cost=Decimal(su["acquired_cost"]),
                    purchase_ref=header["purchase_ref"],
                    photo_reference=su["photo_reference"],
                    # Milestone 5: this purchase-history row still shows the
                    # unit's CURRENT status (a unit purchased in this
                    # transaction may have since been sold) — the purchase's
                    # own cost/quantity figures above are what stay frozen
                    # at their original posted values, not this status flag.
                    status=su["status"],
                    sold_date=su["sold_date"],
                    sold_reference=su["sold_reference"],
                )
                for su in su_rows
            ]
        lines.append(
            PurchaseLineDetail(
                id=lr["id"],
                sku=lr["sku"],
                item_name=lr["item_name"],
                quantity=lr["quantity"],
                pricing_mode=lr["pricing_mode"],
                price_entry_mode=lr["price_entry_mode"],
                price_value=lr["price_value"],
                lumpsum_weight_kg=lr["lumpsum_weight_kg"],
                lumpsum_value=lr["lumpsum_value"],
                ships_separately=lr["ships_separately"],
                manual_shipping_amount=lr["manual_shipping_amount"],
                shipping_weight_kg=lr["shipping_weight_kg"],
                allocated_item_cost=lr["allocated_item_cost"],
                shipping_share=lr["shipping_share"],
                line_total=lr["line_total"],
                identity_mode=lr["identity_mode"],
                serial_units=serial_units,
            )
        )

    header_dict = dict(header)
    line_count = len(lines)
    return PurchaseDetail(
        id=header_dict["id"],
        purchase_ref=header_dict["purchase_ref"],
        purchase_date=header_dict["purchase_date"],
        vendor_description=header_dict["vendor_description"],
        total_amount_paid=header_dict["total_amount_paid"],
        currency=header_dict["currency"],
        shipping_mode=header_dict["shipping_mode"],
        pooled_shipping_total=header_dict["pooled_shipping_total"],
        pooled_shipping_method=header_dict["pooled_shipping_method"],
        lump_sum_active=header_dict["lump_sum_active"],
        lump_sum_total=header_dict["lump_sum_total"],
        lump_sum_method=header_dict["lump_sum_method"],
        invoice_document_ref=header_dict["invoice_document_ref"],
        invoice_ocr_status=header_dict["invoice_ocr_status"],
        invoice_parsed_fields=header_dict["invoice_parsed_fields"],
        line_count=line_count,
        lines=lines,
    )


# --------------------------------------------------------------------- #
# Milestone 5 (sale-side depletion) — read-only surfacing of already-
# posted depletion events for the Sales / Depletion Log screen. Real cost
# figures were already computed once, at depletion time, by
# inventory/depletions.py — this module reads them verbatim, same
# division of labor as everything else in this file (see module
# docstring).
# --------------------------------------------------------------------- #


@dataclass
class DepletionSummary:
    id: int
    depletion_type: str  # 'fungible' | 'serialized'
    depletion_date: date_type
    sku: str
    item_name: str
    quantity: int
    unit_cost: Decimal
    total_cost: Decimal
    reference: Optional[str]


def list_depletions(conn: Connection, sku: Optional[str] = None) -> list[DepletionSummary]:
    """Every depletion event ever posted (fungible + serialized, merged),
    newest first — the Sales / Depletion Log screen's read-only ledger,
    mirroring ``list_purchases``' own shape. ``sku`` optionally narrows to
    one item (used by Item Detail's link into this log, same pattern as
    Item Detail linking into Purchase History by ``purchase_ref``).
    """
    fungible_rows = conn.execute(
        text(
            """
            SELECT fd.id, fd.depletion_date, i.sku, i.name AS item_name,
                   fd.quantity, fd.unit_cost, fd.total_cost, fd.reference
            FROM fungible_depletions fd
            JOIN items i ON i.id = fd.item_id
            WHERE (:sku IS NULL OR UPPER(i.sku) = UPPER(:sku))
            """
        ),
        {"sku": sku},
    ).mappings().all()
    results = [
        DepletionSummary(
            id=r["id"],
            depletion_type="fungible",
            depletion_date=r["depletion_date"],
            sku=r["sku"],
            item_name=r["item_name"],
            quantity=r["quantity"],
            unit_cost=Decimal(r["unit_cost"]),
            total_cost=Decimal(r["total_cost"]),
            reference=r["reference"],
        )
        for r in fungible_rows
    ]

    serial_rows = conn.execute(
        text(
            """
            SELECT su.id, su.sold_date, i.sku, i.name AS item_name,
                   su.acquired_cost, su.sold_reference
            FROM serial_units su
            JOIN items i ON i.id = su.item_id
            WHERE su.status = 'sold'
              AND (:sku IS NULL OR UPPER(i.sku) = UPPER(:sku))
            """
        ),
        {"sku": sku},
    ).mappings().all()
    results += [
        DepletionSummary(
            id=r["id"],
            depletion_type="serialized",
            depletion_date=r["sold_date"],
            sku=r["sku"],
            item_name=r["item_name"],
            quantity=1,
            unit_cost=Decimal(r["acquired_cost"]),
            total_cost=Decimal(r["acquired_cost"]),
            reference=r["sold_reference"],
        )
        for r in serial_rows
    ]

    results.sort(key=lambda d: (d.depletion_date, d.id), reverse=True)
    return results
