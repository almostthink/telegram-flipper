"""Проверки нормализации заголовка авторизации.

У площадок разные схемы, и путать их нельзя: Portals принимает initData
Telegram напрямую, MRKT — только собственный токен, полученный обменом.
Прежняя версия дописывала префикс «tma» ко всему подряд и тем ломала
рабочий токен MRKT, превращая его в заведомо отвергаемый заголовок.
"""

from __future__ import annotations

import pytest
from app.auth.tma import looks_like_init_data, normalize_header
from app.domain import Market

#: Токен MRKT из записи трафика — UUID, никакой схемы.
MRKT_TOKEN = "e66f325a-c7b9-43c0-9e9d-9d2a5df34897"

#: Форма initData Telegram: набор параметров с обязательными hash и auth_date.
INIT_DATA = "query_id=AAA&user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc123"


def test_mrkt_token_is_kept_as_is():
    """Главный случай: UUID нельзя превращать в «tma <uuid>»."""
    assert normalize_header(Market.MRKT, MRKT_TOKEN) == MRKT_TOKEN


def test_mrkt_token_with_spaces_is_trimmed():
    assert normalize_header(Market.MRKT, f"  {MRKT_TOKEN}\n") == MRKT_TOKEN


def test_portals_init_data_gets_tma_prefix():
    assert normalize_header(Market.PORTALS, INIT_DATA) == f"tma {INIT_DATA}"


def test_existing_scheme_is_never_touched():
    for value in (
        f"tma {INIT_DATA}",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "Basic dXNlcjpwYXNz",
        f"Token {MRKT_TOKEN}",
    ):
        assert normalize_header(Market.MRKT, value) == value
        assert normalize_header(Market.PORTALS, value) == value


def test_init_data_for_non_tma_market_is_not_prefixed():
    """Даже похожее на initData значение для MRKT остаётся как есть."""
    assert normalize_header(Market.MRKT, INIT_DATA) == INIT_DATA


def test_empty_token_rejected():
    with pytest.raises(ValueError, match="Пустой"):
        normalize_header(Market.MRKT, "   ")


def test_init_data_detection():
    assert looks_like_init_data(INIT_DATA)
    assert not looks_like_init_data(MRKT_TOKEN)
    # Без auth_date это не initData, а просто строка с hash.
    assert not looks_like_init_data("hash=abc")


# --- Куда уходят учётные данные ------------------------------------------


def test_getgems_uses_cookie_not_authorization():
    """У GetGems нет заголовка Authorization — сессия живёт в cookie.

    Подставлять туда Authorization бессмысленно: площадка его игнорирует,
    и запрос уходит неавторизованным без всякой диагностики.
    """
    from app.adapters.base import AuthPlacement
    from app.adapters.reference import GETGEMS_ENDPOINTS, GetGemsAdapter

    adapter = GetGemsAdapter(
        endpoints=GETGEMS_ENDPOINTS,
        fee_sell=0.05,
        auth_header="AUTH_TOKEN=abc; JWT_TOKEN=xyz",
    )
    assert adapter.auth_placement is AuthPlacement.COOKIE

    headers = adapter._default_headers()
    assert headers["Cookie"] == "AUTH_TOKEN=abc; JWT_TOKEN=xyz"
    assert "Authorization" not in headers


def test_trading_markets_use_authorization_header():
    from app.adapters.base import AuthPlacement
    from app.adapters.mrkt import DEFAULT_ENDPOINTS, MrktAdapter

    adapter = MrktAdapter(
        endpoints=DEFAULT_ENDPOINTS, fee_sell=0.0, auth_header=MRKT_TOKEN
    )
    assert adapter.auth_placement is AuthPlacement.HEADER

    headers = adapter._default_headers()
    assert headers["Authorization"] == MRKT_TOKEN
    assert "Cookie" not in headers


def test_cookie_string_is_stored_verbatim():
    """Строку cookie нельзя ни обрезать, ни дополнять префиксом."""
    cookie = "AUTH_TOKEN=abc; JWT_TOKEN=eyJhbGciOiJIUzI1NiJ9.x.y"
    assert normalize_header(Market.GETGEMS, cookie) == cookie
