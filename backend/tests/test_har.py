"""Проверки восстановления эндпоинтов из HAR.

Это главный механизм починки адаптеров без правки кода: у MRKT нет
опубликованного API, и пути придётся восстанавливать по факту.
"""

from __future__ import annotations

import json

import pytest
from app.adapters.base import MarketEndpoints
from app.adapters.har import classify, find_array_path, parse_har


def entry(url: str, *, body: dict | list, method: str = "GET", auth: str | None = None):
    headers = [{"name": "Accept", "value": "application/json"}]
    if auth:
        headers.append({"name": "Authorization", "value": auth})
    return {
        "request": {"url": url, "method": method, "headers": headers},
        "response": {
            "content": {"mimeType": "application/json", "text": json.dumps(body)}
        },
    }


def har(*entries) -> str:
    return json.dumps({"log": {"entries": list(entries)}})


def test_classify_recognises_common_paths():
    assert classify("/v1/collections/floors") == "collection_floors"
    assert classify("/api/activity") == "activity"
    assert classify("/v1/gifts") == "listings"
    assert classify("/v1/user/balance") == "balance"
    assert classify("/static/logo.png") is None


def test_find_array_path_locates_nested_list():
    assert find_array_path({"data": {"items": [{"a": 1}]}}) == ("data.items", 1)
    assert find_array_path([{"a": 1}, {"b": 2}]) == ("", 2)
    assert find_array_path({"count": 0}) is None


def test_parse_har_extracts_endpoints_and_token():
    document = har(
        entry(
            "https://api.mrkt.xyz/v2/gifts?limit=50",
            body={"items": [{"id": "1", "price": 5}, {"id": "2", "price": 6}]},
            auth="tma user=abc",
        ),
        entry(
            "https://api.mrkt.xyz/v2/collections",
            body={"data": [{"name": "Pepe", "floor": 10}]},
        ),
        entry("https://cdn.example.com/logo.png", body={}),
    )

    result = parse_har(document, host_filter="mrkt")

    assert result.base_url == "https://api.mrkt.xyz"
    assert result.auth_header == "tma user=abc"

    found = {item.endpoint: item for item in result.findings}
    assert found["listings"].path == "/v2/gifts"
    assert found["listings"].json_path == "items"
    assert found["collections"].path == "/v2/collections"
    assert found["collections"].json_path == "data"


def test_parse_har_prefers_richer_response():
    """Из нескольких вызовов одного эндпоинта берём непустой ответ."""
    document = har(
        entry("https://api.mrkt.xyz/v2/gifts", body={"items": []}),
        entry(
            "https://api.mrkt.xyz/v2/gifts",
            body={"items": [{"id": str(i)} for i in range(30)]},
        ),
    )
    result = parse_har(document, host_filter="mrkt")
    listings = next(item for item in result.findings if item.endpoint == "listings")
    assert listings.sample_count == 30


def test_host_filter_excludes_foreign_requests():
    document = har(
        entry("https://telegram.org/api/collections", body={"data": [{"x": 1}]}),
    )
    with pytest.raises(ValueError, match="не найдено"):
        parse_har(document, host_filter="mrkt")


def test_broken_file_reports_clearly():
    with pytest.raises(ValueError, match="корректным HAR"):
        parse_har(b"not json at all")

    with pytest.raises(ValueError, match="log.entries"):
        parse_har(json.dumps({"log": {}}))


def test_result_merges_over_defaults():
    """Найденные пути накладываются поверх дефолтов, не стирая остальные."""
    document = har(
        entry("https://api.mrkt.xyz/v9/gifts", body={"items": [{"id": "1"}]}),
    )
    result = parse_har(document, host_filter="mrkt")

    from app.adapters.mrkt import DEFAULT_ENDPOINTS

    merged: MarketEndpoints = result.to_endpoints(DEFAULT_ENDPOINTS)

    assert merged.endpoints["listings"].path == "/v9/gifts"
    # Незатронутые эндпоинты остаются от значений по умолчанию.
    assert merged.endpoints["balance"].path == DEFAULT_ENDPOINTS.endpoints["balance"].path


def test_falls_back_when_host_filter_matches_nothing():
    """API площадки может жить на домене без её имени в адресе.

    Молча возвращать «ничего не найдено» в такой ситуации нельзя —
    пользователь не поймёт, что фильтр по домену и есть причина.
    """
    document = har(
        entry("https://api.tg-gifts-backend.io/v1/gifts", body={"items": [{"id": "1"}]}),
        entry("https://web.telegram.org/api/collections", body={"data": [{"x": 1}]}),
    )
    result = parse_har(document, host_filter="mrkt")

    assert result.matched_by_fallback is True
    assert result.base_url == "https://api.tg-gifts-backend.io"
    # Инфраструктурные домены в выдачу попадать не должны.
    assert all("telegram.org" not in item.base_url for item in result.findings)


def test_error_lists_seen_hosts_to_explain_failure():
    document = har(entry("https://cdn.example.com/config", body={"theme": "dark"}))
    with pytest.raises(ValueError) as exc:
        parse_har(document, host_filter="mrkt")

    # В сообщении должно быть видно, куда парсер вообще смотрел.
    assert "cdn.example.com" in str(exc.value)


def test_filter_still_wins_when_it_matches():
    """Фолбэк не должен срабатывать, если по фильтру что-то нашлось."""
    document = har(
        entry("https://api.mrkt.xyz/v1/gifts", body={"items": [{"id": "1"}]}),
        entry(
            "https://other.example.com/v1/gifts",
            body={"items": [{"id": str(i)} for i in range(50)]},
        ),
    )
    result = parse_har(document, host_filter="mrkt")

    assert result.matched_by_fallback is False
    assert result.base_url == "https://api.mrkt.xyz"
