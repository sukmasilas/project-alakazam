"""Fuzzy title-match suggestion logic — advisory only (never auto-applied,
see CLAUDE.md), but still needs to actually rank real candidates sensibly
against real Postgres data.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase
from ingestion.fuzzy_match import suggest_item_matches, _score


def _create_item(conn, name, category_code="TCG", identity_mode="fungible"):
    result = save_purchase(
        conn,
        PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description=f"Stock-up: {name}",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=NewItemInput(name=name, category_code=category_code, identity_mode=identity_mode),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("100000"),
                )
            ],
        ),
    )
    return result.lines[0].sku


class TestScoreFunction:
    def test_identical_strings_score_highest(self):
        assert _score("Rolex Submariner", "Rolex Submariner") > 0.9

    def test_completely_unrelated_strings_score_low(self):
        assert _score("Rolex Submariner Watch", "Brake Pad Set") < 0.2

    def test_empty_strings_score_zero(self):
        assert _score("", "Rolex Submariner") == 0.0
        assert _score("Rolex Submariner", "") == 0.0

    def test_noisy_real_world_title_still_scores_the_right_catalog_name_highly(self):
        ebay_title = "Pokemon Indonesia Cynthia's Garchomp ex SV10s 178/138 SAR SIR Destined Rivals"
        catalog_name = "Cynthia's Garchomp ex"
        wrong_name = "Barrel of Monkeys Toy"
        assert _score(ebay_title, catalog_name) > _score(ebay_title, wrong_name)
        assert _score(ebay_title, catalog_name) >= 0.35


class TestSuggestItemMatches:
    def test_suggests_the_real_matching_item_first(self, conn):
        _create_item(conn, "Rolex Submariner", category_code="WATCHES", identity_mode="serialized")
        _create_item(conn, "Brake Pad Set", category_code="AUTOMOTIVE")
        _create_item(conn, "Omega Speedmaster", category_code="WATCHES", identity_mode="serialized")

        candidates = suggest_item_matches(conn, "Rolex Submariner Date Stainless Steel Watch Box Papers")
        assert len(candidates) >= 1
        assert candidates[0].name == "Rolex Submariner"
        assert candidates[0].identity_mode == "serialized"

    def test_no_suggestion_when_nothing_scores_above_threshold(self, conn):
        _create_item(conn, "Brake Pad Set", category_code="AUTOMOTIVE")
        candidates = suggest_item_matches(conn, "Totally Unrelated Pokemon Card Listing Title")
        assert candidates == []

    def test_limit_caps_the_number_of_suggestions(self, conn):
        for i in range(5):
            _create_item(conn, f"Pokemon Booster Pack Variant {i}", category_code="TCG")
        candidates = suggest_item_matches(conn, "Pokemon Booster Pack Variant", limit=2)
        assert len(candidates) <= 2

    def test_empty_catalog_returns_no_suggestions(self, conn):
        candidates = suggest_item_matches(conn, "Anything at all")
        assert candidates == []
