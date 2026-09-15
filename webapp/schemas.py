"""Pydantic request/response models for the JSON API.

These are pure request/response shape definitions — every actual
computation (allocation, SKU/serial generation, uniqueness) still happens
in ``inventory/*.py``. ``to_purchase_input()`` below is a straight field
mapping into ``inventory.purchases.PurchaseInput`` and friends, not a
reimplementation of any business rule.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from inventory import purchases as purchases_engine


class NewItemIn(BaseModel):
    name: str
    category_code: str
    identity_mode: str
    sku: Optional[str] = None


class SerialUnitIn(BaseModel):
    serial_id: Optional[str] = None
    cost: Optional[Decimal] = None
    photo_reference: Optional[str] = None


class PurchaseLineIn(BaseModel):
    sku: Optional[str] = None
    new_item: Optional[NewItemIn] = None
    quantity: int = 1

    pricing_mode: str = "direct"
    price_entry_mode: Optional[str] = None
    price_value: Optional[Decimal] = None
    lumpsum_weight_kg: Optional[Decimal] = None
    lumpsum_value: Optional[Decimal] = None

    ships_separately: bool = False
    manual_shipping_amount: Optional[Decimal] = None
    shipping_weight_kg: Optional[Decimal] = None

    serial_units: Optional[list[SerialUnitIn]] = None


class PurchaseIn(BaseModel):
    purchase_date: date
    vendor_description: str
    total_amount_paid: Decimal
    currency: str = "IDR"
    fx_rate_to_idr: Optional[Decimal] = None

    shipping_mode: str = "none"
    pooled_shipping_total: Optional[Decimal] = None
    pooled_shipping_method: Optional[str] = None

    lump_sum_active: bool = False
    lump_sum_total: Optional[Decimal] = None
    lump_sum_method: Optional[str] = None

    invoice_document_ref: Optional[str] = None
    invoice_ocr_status: Optional[str] = None
    invoice_parsed_fields: Optional[dict] = None

    lines: list[PurchaseLineIn] = Field(default_factory=list)


def to_purchase_input(body: PurchaseIn) -> purchases_engine.PurchaseInput:
    """Straight field-for-field mapping from the wire format into the real
    engine's dataclasses (inventory/purchases.py) — no computation here.
    """
    lines = []
    for line in body.lines:
        new_item = None
        if line.new_item is not None:
            new_item = purchases_engine.NewItemInput(
                name=line.new_item.name,
                category_code=line.new_item.category_code,
                identity_mode=line.new_item.identity_mode,
                sku=line.new_item.sku,
            )
        serial_units = None
        if line.serial_units is not None:
            serial_units = [
                purchases_engine.SerialUnitInput(
                    serial_id=su.serial_id, cost=su.cost, photo_reference=su.photo_reference
                )
                for su in line.serial_units
            ]
        lines.append(
            purchases_engine.PurchaseLineInput(
                sku=line.sku,
                new_item=new_item,
                quantity=line.quantity,
                pricing_mode=line.pricing_mode,
                price_entry_mode=line.price_entry_mode,
                price_value=line.price_value,
                lumpsum_weight_kg=line.lumpsum_weight_kg,
                lumpsum_value=line.lumpsum_value,
                ships_separately=line.ships_separately,
                manual_shipping_amount=line.manual_shipping_amount,
                shipping_weight_kg=line.shipping_weight_kg,
                serial_units=serial_units,
            )
        )

    return purchases_engine.PurchaseInput(
        purchase_date=body.purchase_date,
        vendor_description=body.vendor_description,
        total_amount_paid=body.total_amount_paid,
        currency=body.currency,
        fx_rate_to_idr=body.fx_rate_to_idr,
        shipping_mode=body.shipping_mode,
        pooled_shipping_total=body.pooled_shipping_total,
        pooled_shipping_method=body.pooled_shipping_method,
        lump_sum_active=body.lump_sum_active,
        lump_sum_total=body.lump_sum_total,
        lump_sum_method=body.lump_sum_method,
        invoice_document_ref=body.invoice_document_ref,
        invoice_ocr_status=body.invoice_ocr_status,
        invoice_parsed_fields=body.invoice_parsed_fields,
        lines=lines,
    )


class NewItemCreateIn(BaseModel):
    name: str
    category_code: str
    identity_mode: str
    sku: Optional[str] = None


# --------------------------------------------------------------------- #
# Milestone 5 — sale-side depletion
# --------------------------------------------------------------------- #


class FungibleDepletionIn(BaseModel):
    quantity: int
    reference: Optional[str] = None
    depletion_date: Optional[date] = None


class SerialDepletionIn(BaseModel):
    reference: Optional[str] = None
    sold_date: Optional[date] = None


# --------------------------------------------------------------------- #
# Milestone 6 — eBay sales CSV import / review queue
# --------------------------------------------------------------------- #


class EbayRowMatchIn(BaseModel):
    sku: str
    serial_ids: Optional[list[str]] = None


# --------------------------------------------------------------------- #
# Milestone 7 — pre-order/dropship sales
# --------------------------------------------------------------------- #


class PreorderSaleCreateIn(BaseModel):
    sku: str
    quantity: int
    sale_date: Optional[date] = None
    reference: Optional[str] = None


class PreorderFulfillIn(BaseModel):
    preorder_sale_ids: list[int]
    # Exactly one of the two below must be set — enforced server-side by
    # inventory.preorders.fulfill_preorder_sales(), never trusted from the
    # client. ``purchase`` reuses the SAME ``PurchaseIn`` shape as
    # /api/purchases (see ``to_purchase_input`` above) — no separate,
    # simplified purchase schema, so a fulfillment purchase gets the exact
    # same real validation/allocation engine as any other purchase.
    purchase: Optional[PurchaseIn] = None
    purchase_id: Optional[int] = None


# --------------------------------------------------------------------- #
# Milestone 8 — consignment tracking
# --------------------------------------------------------------------- #


class ConsignorCreateIn(BaseModel):
    name: str
    contact_info: Optional[str] = None


class ConsignmentIntakeIn(BaseModel):
    consignor_id: int

    # Exactly one of the two below must be given — enforced server-side by
    # inventory.consignment.intake_consigned_units(), never trusted from
    # the client.
    sku: Optional[str] = None
    new_item_name: Optional[str] = None
    new_item_category_code: Optional[str] = None
    new_item_sku: Optional[str] = None

    quantity: int = 1
    serial_ids: Optional[list[str]] = None
    photo_references: Optional[list[str]] = None


class ReimbursementMarkPaidIn(BaseModel):
    paid_date: Optional[date] = None
    payment_reference: Optional[str] = None
