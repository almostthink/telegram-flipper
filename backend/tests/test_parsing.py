"""Проверки разбора ответов площадок.

Схема у площадок не опубликована и менялась, поэтому разбор построен на
списках вероятных имён полей. Тесты фиксируют, что все известные формы
распознаются.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.adapters.parsing import as_list, parse_gift, pick, to_datetime, to_ton
from app.domain import AttributeKind


def test_pick_returns_first_present_value():
    assert pick({"b": 2}, "a", "b") == 2
    assert pick({"a": None, "b": ""}, "a", "b", default="x") == "x"


def test_to_ton_handles_nanotons():
    assert to_ton(5.5) == 5.5
    assert to_ton("12.25") == 12.25
    # 5 TON в нанотонах
    assert to_ton(5_000_000_000) == 5.0
    assert to_ton(0) is None
    assert to_ton("нет") is None


def test_to_datetime_handles_all_formats():
    assert to_datetime("2026-03-01T10:00:00Z") == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert to_datetime(1772000000).tzinfo is UTC
    # Миллисекунды
    assert to_datetime(1772000000000).year == to_datetime(1772000000).year
    assert to_datetime("мусор") is None


def test_parse_gift_flat_format():
    gift = parse_gift(
        {
            "id": "nft-1",
            "collection_name": "Plush Pepe",
            "external_collection_number": 42,
            "model": {"name": "Golden", "rarity_per_mille": 5},
            "backdrop": "Ocean",
        }
    )

    assert gift.collection == "Plush Pepe"
    assert gift.external_id == "nft-1"
    assert gift.number == 42
    assert gift.model.name == "Golden"
    assert gift.model.rarity_permille == 5
    assert gift.backdrop.name == "Ocean"
    assert gift.symbol is None


def test_parse_gift_attributes_list_format():
    """Формат GetGems: атрибуты списком с trait_type."""
    gift = parse_gift(
        {
            "id": "x",
            "name": "Cake",
            "attributes": [
                {"trait_type": "Model", "value": "Rare", "rarity": 0.02},
                {"trait_type": "Background", "value": "Blue"},
                {"trait_type": "Pattern", "value": "Star"},
            ],
        }
    )

    assert gift.model.name == "Rare"
    # Долю 0.02 приводим к промилле.
    assert gift.model.rarity_permille == 20
    assert gift.backdrop.name == "Blue"
    assert gift.symbol.name == "Star"


def test_rarity_fraction_conversion():
    gift = parse_gift({"model": {"name": "M", "rarity_per_mille": 25}})
    assert gift.model.rarity_fraction == 0.025


def test_slice_key_groups_by_model():
    a = parse_gift({"collection_name": "C", "model": "Epic", "backdrop": "X"})
    b = parse_gift({"collection_name": "C", "model": "Epic", "backdrop": "Y"})
    c = parse_gift({"collection_name": "C", "model": "Rare"})

    assert a.slice_key == b.slice_key
    assert a.slice_key != c.slice_key


def test_as_list_finds_array_anywhere():
    assert as_list([{"a": 1}]) == [{"a": 1}]
    assert as_list({"results": [{"a": 1}]}) == [{"a": 1}]
    assert as_list({"nfts": [{"a": 1}]}) == [{"a": 1}]
    assert as_list({"nothing": 1}) == []


def test_attribute_kinds_assigned_correctly():
    gift = parse_gift({"model": "M", "backdrop": "B", "symbol": "S"})
    assert gift.model.kind is AttributeKind.MODEL
    assert gift.backdrop.kind is AttributeKind.BACKDROP
    assert gift.symbol.kind is AttributeKind.SYMBOL
