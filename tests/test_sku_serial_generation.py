"""Pure unit tests for inventory/sku.py and inventory/serials.py — no
database needed. Uniqueness enforcement itself (the real gate) is tested
against a real database in tests/test_purchases.py, per the brief's
explicit instruction not to test generation-avoids-collisions alone.
"""
from __future__ import annotations

from inventory.serials import generate_serial_id, generate_serials_for_line
from inventory.sku import generate_sku, slugify_name


class TestSlugifyName:
    def test_first_two_words_only(self):
        assert slugify_name("Charizard VMAX Box") == "CHARIZARD-VMAX"

    def test_single_word(self):
        assert slugify_name("Rolex") == "ROLEX"

    def test_non_alnum_collapsed_to_single_hyphen(self):
        slug = slugify_name("Item---With   Spaces")
        assert not slug.startswith("-")
        assert not slug.endswith("-")
        assert "--" not in slug

    def test_empty_name_falls_back_to_item(self):
        assert slugify_name("") == "ITEM"
        assert slugify_name("   ") == "ITEM"


class TestGenerateSku:
    def test_first_sku_for_a_new_family_starts_at_0001(self):
        assert generate_sku("TCG", "Charizard VMAX Box", existing_skus=[]) == "TCG-CHARIZARD-VMAX-0001"

    def test_sequence_scoped_to_prefix_and_slug_combination(self):
        existing = ["TCG-CHARIZARD-VMAX-0001", "TCG-YUGIOH-STARTER-DECK-0001"]
        # A second, unrelated TCG item does not bump the Charizard family's counter.
        assert generate_sku("TCG", "Yugioh Starter Deck", existing) == "TCG-YUGIOH-STARTER-0001"
        # A second Charizard VMAX item correctly increments.
        assert generate_sku("TCG", "Charizard VMAX Box", existing) == "TCG-CHARIZARD-VMAX-0002"

    def test_case_insensitive_matching_against_existing_skus(self):
        existing = ["tcg-charizard-vmax-0003"]
        assert generate_sku("TCG", "Charizard VMAX Box", existing) == "TCG-CHARIZARD-VMAX-0004"

    def test_different_category_prefix_is_a_different_family_even_with_same_slug(self):
        existing = ["TCG-CHARIZARD-VMAX-0001"]
        assert generate_sku("TOY", "Charizard VMAX Plush", existing) == "TOY-CHARIZARD-VMAX-0001"


class TestGenerateSerialId:
    def test_pads_to_three_digits(self):
        assert generate_serial_id("TCG-CHARIZARD-1ED-0001", 1) == "TCG-CHARIZARD-1ED-0001-001"
        assert generate_serial_id("TCG-CHARIZARD-1ED-0001", 42) == "TCG-CHARIZARD-1ED-0001-042"


class TestGenerateSerialsForLine:
    def test_continues_from_existing_count(self):
        result = generate_serials_for_line("AUTO-ECU-HONDA-0001", quantity=3, existing_count=2)
        assert result == [
            "AUTO-ECU-HONDA-0001-003",
            "AUTO-ECU-HONDA-0001-004",
            "AUTO-ECU-HONDA-0001-005",
        ]

    def test_fresh_item_starts_at_001(self):
        result = generate_serials_for_line("AUTO-ECU-HONDA-0001", quantity=2, existing_count=0)
        assert result == ["AUTO-ECU-HONDA-0001-001", "AUTO-ECU-HONDA-0001-002"]

    def test_sibling_reserved_serials_prevent_same_purchase_collision(self):
        # Two lines of the same SKU in one purchase: line A (qty 2) is
        # generated first, then line B (qty 1) must continue past line A's
        # freshly-generated serials, not restart from the stale DB count.
        sku = "TCG-PIKACHU-VMAX-0001"
        line_a = generate_serials_for_line(sku, quantity=2, existing_count=0)
        assert line_a == [f"{sku}-001", f"{sku}-002"]
        line_b = generate_serials_for_line(sku, quantity=1, existing_count=0, sibling_reserved=line_a)
        assert line_b == [f"{sku}-003"]
        assert len(set(line_a + line_b)) == 3  # no collision
