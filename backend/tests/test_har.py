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


def test_correct_filter_never_sees_foreign_hosts():
    """При верном фильтре посторонние домены до разбора вообще не доходят."""
    document = har(
        entry("https://portal-market.com/api/collections", body={"collections": [{"a": 1}]}),
        entry("https://config.ton.org/wallets-v2.json", body={"balance": [{"c": 3}]}),
    )
    result = parse_har(document, host_filter="portal-market")

    assert result.base_url == "https://portal-market.com"
    assert result.matched_by_fallback is False
    assert "balance" not in {item.endpoint for item in result.findings}


def test_rejected_hosts_are_reported_on_fallback():
    """Отбраковка важна именно на пути фолбэка — там она и понадобилась.

    При промахе фильтра разбор идёт по всем доменам сразу, и без отбраковки
    служебный файл чужого сервиса объявляется эндпоинтом баланса.
    """
    document = har(
        entry("https://portal-market.com/api/collections", body={"collections": [{"a": 1}]}),
        entry("https://portal-market.com/api/nfts/search", body={"results": [{"b": 2}]}),
        entry("https://config.ton.org/wallets.json", body={"balance": [{"c": 3}]}),
    )
    # Промахнувшийся фильтр — ровно та опечатка, что была в коде.
    result = parse_har(document, host_filter="portals")

    assert result.matched_by_fallback is True
    assert result.base_url == "https://portal-market.com"
    assert "https://config.ton.org" in result.rejected_hosts
    assert "balance" not in {item.endpoint for item in result.findings}


# --- Тела торговых запросов -----------------------------------------------
#
# Второй смысл HAR: пути мини-апп раскрывает своим кодом, а состав тела —
# нет, форма собирает поля у себя. Увидеть тело можно только в записи, и
# отдать его нужно так, чтобы вместе с ним не уехали учётные данные.


def post(url: str, body: dict, response: dict | list | None = None, method: str = "POST"):
    return {
        "request": {
            "url": url,
            "method": method,
            "headers": [{"name": "Authorization", "value": "secret-token"}],
            "postData": {"mimeType": "application/json", "text": json.dumps(body)},
        },
        "response": {
            "content": {
                "mimeType": "application/json",
                "text": json.dumps(response if response is not None else {"ok": True}),
            }
        },
    }


def test_trade_action_is_recognised_by_path_and_method():
    from app.adapters.har import is_trade_action

    assert is_trade_action("POST", "/api/v1/orders/create")
    assert is_trade_action("POST", "/api/v1/gifts/buy")
    assert is_trade_action("POST", "/api/auction/bid")
    # Читающие запросы бывают и POST — у MRKT так устроен список лотов.
    assert not is_trade_action("POST", "/api/v1/gifts")
    assert not is_trade_action("GET", "/api/v1/orders/all-collection-top")
    # Обмен initData на токен — учётные данные целиком, а не сделка.
    assert not is_trade_action("POST", "/api/v1/auth")


def test_trade_bodies_are_extracted():
    result = parse_har(
        har(
            entry("https://api.tgmrkt.io/api/v1/gifts", body={"gifts": [{"id": 1}]}),
            post(
                "https://api.tgmrkt.io/api/v1/orders/create",
                {"collectionId": "abc", "price": 1500000000, "amount": 2},
                {"orderId": "O-1"},
            ),
        ),
        host_filter="tgmrkt",
    )

    assert len(result.trade_calls) == 1
    call = result.trade_calls[0]
    assert call.path == "/api/v1/orders/create"
    assert call.request == {"collectionId": "abc", "price": 1500000000, "amount": 2}
    assert call.response == {"orderId": "O-1"}


def test_extracted_bodies_carry_no_credentials():
    """Тела показываются человеку и пересылаются — доступа в них быть не должно."""
    result = parse_har(
        har(
            post(
                "https://gifts2.tonnel.network/api/auction/bid",
                {
                    "authData": "query_id=AAA&user=%7B%22id%22%3A1%7D&hash=abc",
                    "gift_id": 77,
                    "amount": 4.2,
                },
            )
        ),
        host_filter="tonnel",
    )

    body = result.trade_calls[0].request
    assert body["gift_id"] == 77, "параметры сделки должны сохраниться"
    assert "query_id" not in json.dumps(body, ensure_ascii=False)
    assert "hash=abc" not in json.dumps(body, ensure_ascii=False)


def test_init_data_is_cut_wherever_it_hides():
    from app.adapters.har import sanitize

    cleaned = sanitize({"payload": {"any_name": "auth_date=1700000000&hash=zz"}})
    assert cleaned["payload"]["any_name"] == "<initData вырезан>"


def test_huge_values_are_trimmed():
    """Ради них HAR и раздувается: base64 с картинкой в теле запроса."""
    from app.adapters.har import sanitize

    cleaned = sanitize({"photo": "A" * 5000})
    assert len(cleaned["photo"]) < 200
    assert "обрезано" in cleaned["photo"]


def test_har_with_only_trade_actions_is_not_rejected():
    """Запись второго захода может состоять из одних действий — это нормально."""
    result = parse_har(
        har(post("https://api.tgmrkt.io/api/v1/gifts/buy", {"ids": ["G1"]})),
        host_filter="tgmrkt",
    )
    assert result.trade_calls
    assert result.findings == []


def test_buy_never_overwrites_the_listings_path():
    """Путь /gifts/buy содержит «gift» — и однажды подменил бы список лотов.

    Тогда импорт HAR, записанного во время торговли, ломал бы сбор
    данных: приложение ходило бы за списком подарков на адрес покупки.
    """
    result = parse_har(
        har(
            entry("https://api.tgmrkt.io/api/v1/gifts", body={"gifts": [{"id": 1}]}),
            post("https://api.tgmrkt.io/api/v1/gifts/buy", {"ids": ["G1"]}),
        ),
        host_filter="tgmrkt",
    )

    listings = next(f for f in result.findings if f.endpoint == "listings")
    assert listings.path == "/api/v1/gifts"


# --- Загрузка архивом -----------------------------------------------------
#
# HAR — текст, zip ужимает его раз в пятнадцать. Для записи торговой
# сессии это единственный способ передать её целиком: базовый снимок
# подарков сам по себе занимает больше прежнего предела.


def zipped(payload: str, name: str = "trace.har") -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


def test_zip_is_unpacked():
    from app.api.routes_auth import _unpack

    payload = har(entry("https://api.tgmrkt.io/v1/gifts", body={"gifts": [{"id": 1}]}))
    assert _unpack(zipped(payload), "trace.zip") == payload.encode()


def test_plain_har_passes_through_untouched():
    from app.api.routes_auth import _unpack

    payload = har(entry("https://api.tgmrkt.io/v1/gifts", body={"gifts": [{"id": 1}]}))
    assert _unpack(payload.encode(), "trace.har") == payload.encode()


def test_biggest_member_wins():
    """Рядом с записью в архив попадают мелкие служебные json."""
    import io
    import zipfile

    from app.api.routes_auth import _unpack

    payload = har(entry("https://api.tgmrkt.io/v1/gifts", body={"gifts": [{"id": 1}]}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("meta.json", "{}")
        archive.writestr("trace.har", payload)

    assert _unpack(buffer.getvalue(), "trace.zip") == payload.encode()


def test_archive_without_har_is_reported():
    import io
    import zipfile

    from app.api.routes_auth import _unpack

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "нет тут ничего")

    with pytest.raises(ValueError, match="нет файла"):
        _unpack(buffer.getvalue(), "trace.zip")


def test_zip_bomb_is_stopped_before_unpacking():
    """Архив в пару мегабайт разворачивается в гигабайты и кладёт приложение.

    Размер объявлен в оглавлении архива, поэтому проверить его можно, не
    распаковывая, — этим и пользуемся.
    """
    import io
    import zipfile

    from app.api.routes_auth import MAX_HAR_BYTES, _unpack

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.har", b"\0" * (MAX_HAR_BYTES + 1))

    with pytest.raises(ValueError, match="предел"):
        _unpack(buffer.getvalue(), "bomb.zip")


def test_broken_archive_is_reported():
    from app.api.routes_auth import _unpack

    with pytest.raises(ValueError, match="повреждён"):
        _unpack(b"PK\x03\x04" + "мусор".encode(), "trace.zip")
