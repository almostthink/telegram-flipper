"""Общий интерфейс площадки и HTTP-клиент с бережным rate-limit.

Все адаптеры реализуют ``Marketplace``. Аналитика и торговый движок
работают только через этот интерфейс — добавить пятую площадку означает
написать один файл, не трогая остальное.

Эндпоинты вынесены в ``EndpointSpec`` и переопределяются из конфига:
у MRKT и Portals нет официального API, их URL могут смениться в любой
момент, и чинить это перекомпиляцией exe неприемлемо.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.domain import (
    ActivityEvent,
    AttributeFloor,
    Balance,
    CollectionFloor,
    CollectionOffer,
    Listing,
    Market,
    MarketOrder,
    OwnedGift,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
MAX_RETRIES = 3


class MarketplaceError(RuntimeError):
    """Ошибка обращения к площадке."""


class AuthExpired(MarketplaceError):
    """Токен протух — нужно обновить и повторить."""


class RateLimited(MarketplaceError):
    """Площадка попросила притормозить."""


@dataclass(slots=True)
class EndpointSpec:
    """Описание одного эндпоинта: путь, метод и способ разбора ответа.

    ``json_path`` — путь к массиву данных внутри ответа, точками
    (например ``results.items``). Пустая строка означает корень.
    """

    path: str
    method: str = "GET"
    json_path: str = ""
    #: Свой домен для этого эндпоинта. Пусто — берётся общий ``base_url``.
    #: Нужен там, где площадка держит чтение и торговлю на разных серверах:
    #: у Tonnel это буквально разные домены, и запрос на покупку по адресу
    #: читающего сервера просто не доходит.
    base_url: str = ""


@dataclass(slots=True)
class MarketEndpoints:
    base_url: str
    endpoints: dict[str, EndpointSpec] = field(default_factory=dict)
    #: Адрес хранилища картинок и анимаций. Отдельно от API: у MRKT это
    #: другой домен, а ключи анимаций в ответах даны относительно него.
    #: Правится так же, как остальные адреса, без пересборки.
    cdn_url: str = ""

    def get(self, name: str) -> EndpointSpec:
        spec = self.endpoints.get(name)
        if spec is None:
            raise MarketplaceError(
                f"Эндпоинт '{name}' не настроен. "
                f"Импортируйте HAR-файл в Настройках, чтобы восстановить адрес."
            )
        return spec


def dig(payload: Any, json_path: str) -> Any:
    """Достаём вложенное значение по пути вида 'data.items'."""
    if not json_path:
        return payload
    current = payload
    for part in json_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


#: Во сколько раз замедляемся после отказа по частоте.
SLOWDOWN_FACTOR = 1.6
#: Потолок паузы. Дальше замедляться бессмысленно: проход всё равно
#: не уложится в свой период, а данные устареют.
MAX_INTERVAL_SEC = 10.0
#: Сколько удачных запросов подряд нужно, чтобы ускориться на шаг назад.
RELAX_AFTER_OK = 20


class RateLimiter:
    """Пауза между запросами: базовая, плюс джиттер, плюс адаптация.

    Ровный интервал запросов выглядит как бот. Джиттер и случайная задержка
    делают трафик менее машинным, а заодно разводят параллельные адаптеры.

    Фиксированной паузы недостаточно. Сколько запросов в минуту площадка
    терпит — не документировано и меняется, а упереться в её лимит хуже,
    чем идти медленнее: три повтора подряд получают тот же отказ, запрос
    признаётся неудачным, и целый источник данных пропадает из прохода.
    Поэтому на 429 пауза растёт и держится, а возвращается к базовой лишь
    после череды удачных запросов.
    """

    def __init__(self, min_interval_sec: float) -> None:
        self.base_interval = min_interval_sec
        self.min_interval = min_interval_sec
        self._last = 0.0
        self._ok_streak = 0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last
            delay = self.min_interval - elapsed
            jitter = random.uniform(0, self.min_interval * 0.4)
            if delay + jitter > 0:
                await asyncio.sleep(max(delay, 0) + jitter)
            self._last = loop.time()

    def penalize(self) -> None:
        """Площадка попросила притормозить — тормозим до следующего разгона."""
        self._ok_streak = 0
        slowed = min(max(self.min_interval, self.base_interval) * SLOWDOWN_FACTOR,
                     MAX_INTERVAL_SEC)
        if slowed > self.min_interval:
            self.min_interval = slowed
            log.info("Пауза между запросами увеличена до %.1fс", self.min_interval)

    def relax(self) -> None:
        """Череда удачных запросов — можно осторожно ускориться."""
        if self.min_interval <= self.base_interval:
            return
        self._ok_streak += 1
        if self._ok_streak < RELAX_AFTER_OK:
            return
        self._ok_streak = 0
        self.min_interval = max(self.base_interval, self.min_interval / SLOWDOWN_FACTOR)
        log.info("Пауза между запросами снижена до %.1fс", self.min_interval)


class AuthPlacement(StrEnum):
    """Куда площадка ждёт учётные данные.

    Единой схемы нет, и подставлять всем Authorization неверно: GetGems
    держит сессию в cookie (AUTH_TOKEN и JWT_TOKEN), и заголовок
    авторизации там попросту игнорируется.
    """

    HEADER = "header"
    COOKIE = "cookie"
    #: Tonnel не использует заголовок вовсе: initData Telegram лежит прямо
    #: в JSON запроса. Именно поэтому Authorization в её трафике не найти.
    BODY = "body"


class Marketplace(ABC):
    """Площадка. Чтение обязательно, торговля — только там, где разрешена."""

    name: Market
    #: Умеет ли адаптер покупать и продавать, а не только читать цены.
    supports_trading: bool = False
    #: Есть ли на площадке книга заявок по коллекции — «куплю любой подарок
    #: из этой коллекции по такой цене». Именно с ней работает ордер-движок.
    #: Там, где заявка адресуется конкретному экземпляру (Tonnel), такой
    #: книги нет, и вставать в очередь по коллекции попросту негде.
    supports_collection_orders: bool = False
    #: Куда класть учётные данные. Переопределяется в адаптере площадки.
    auth_placement: AuthPlacement = AuthPlacement.HEADER

    def __init__(
        self,
        endpoints: MarketEndpoints,
        *,
        fee_sell: float,
        fee_buy: float = 0.0,
        request_delay_sec: float = 1.0,
        auth_header: str | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.endpoints = endpoints
        self.fee_sell = fee_sell
        self.fee_buy = fee_buy
        self.auth_header = auth_header
        # Лимит частоты — свойство аккаунта, а не объекта. Адаптеры
        # создаются заново на каждый проход, и собственный счётчик у
        # каждого означал бы, что выученное замедление тут же забывается.
        self._limiter = limiter or RateLimiter(request_delay_sec)
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None

    # --- Жизненный цикл -------------------------------------------------

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.endpoints.base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._default_headers(),
                follow_redirects=True,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _auth_header_name(self) -> str:
        return "Cookie" if self.auth_placement is AuthPlacement.COOKIE else "Authorization"

    @property
    def _sends_auth_header(self) -> bool:
        return self.auth_placement is not AuthPlacement.BODY

    def set_auth(self, header_value: str | None) -> None:
        """Обновляем токен без пересоздания клиента."""
        self.auth_header = header_value
        if self._client is not None:
            if header_value:
                self._client.headers[self._auth_header_name] = header_value
            else:
                self._client.headers.pop(self._auth_header_name, None)

    #: Под каким именем класть учётные данные в тело. У Tonnel их два:
    #: pageGifts ждёт user_auth, остальные — authData.
    body_auth_field: str = "authData"
    body_auth_overrides: dict[str, str] = {}

    def _inject_body_auth(self, endpoint: str, json_body: dict | None) -> dict | None:
        if self.auth_placement is not AuthPlacement.BODY or not self.auth_header:
            return json_body
        field = self.body_auth_overrides.get(endpoint, self.body_auth_field)
        return {**(json_body or {}), field: self.auth_header}

    def _default_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            # Мини-аппы открываются во встроенном браузере Telegram —
            # обычный десктопный UA выглядит для них естественно.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if self.auth_header and self._sends_auth_header:
            headers[self._auth_header_name] = self.auth_header
        return headers

    # --- Транспорт ------------------------------------------------------

    async def request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw: bool = False,
        path_params: dict[str, Any] | None = None,
    ) -> Any:
        """Запрос с ретраями, разбором ошибок авторизации и rate-limit.

        ``raw=True`` отдаёт ответ целиком, без выборки массива по
        ``json_path``. Это нужно постраничным обходам: курсор следующей
        страницы лежит рядом с массивом, и после выборки он теряется.
        """
        spec = self.endpoints.get(endpoint)
        # Часть адресов содержит идентификатор прямо в пути: отмена ордера
        # адресуется как /orders/cancel/<id>, тела у неё нет.
        path = spec.path.format(**path_params) if path_params else spec.path
        if spec.base_url:
            # httpx: абсолютный адрес перекрывает base_url клиента.
            path = spec.base_url.rstrip("/") + path
        json_body = self._inject_body_auth(endpoint, json_body)
        await self.open()
        assert self._client is not None

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            await self._limiter.wait()
            try:
                response = await self._client.request(
                    spec.method, path, params=params, json=json_body
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code in (401, 403):
                self.last_error = "авторизация отклонена"
                raise AuthExpired(f"{self.name}: токен недействителен ({response.status_code})")

            if response.status_code == 429:
                # Замедляемся не только сейчас, но и на будущие запросы:
                # иначе следующий проход упрётся в тот же лимит.
                self._limiter.penalize()
                # Уважаем Retry-After, если площадка его прислала.
                pause = float(response.headers.get("Retry-After", 2**attempt))
                log.warning("%s: rate limit, пауза %.1fс", self.name, pause)
                await asyncio.sleep(min(pause, 60))
                last_exc = RateLimited(f"{self.name}: 429")
                continue

            if response.status_code >= 500:
                last_exc = MarketplaceError(f"{self.name}: сервер вернул {response.status_code}")
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                self.last_error = f"HTTP {response.status_code}"
                raise MarketplaceError(
                    f"{self.name}: {response.status_code} на {path} — {response.text[:200]}"
                )

            self.last_error = None
            self._limiter.relax()
            try:
                payload = response.json()
            except ValueError as exc:
                raise MarketplaceError(f"{self.name}: ответ не JSON на {path}") from exc
            return payload if raw else dig(payload, spec.json_path)

        self.last_error = str(last_exc)
        raise MarketplaceError(f"{self.name}: не удалось выполнить {endpoint}: {last_exc}")

    # --- Чтение рынка (обязательно) --------------------------------------

    @abstractmethod
    async def collection_floors(self) -> list[CollectionFloor]:
        """Флоры и объёмы по всем коллекциям."""

    @abstractmethod
    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        """Активные листинги коллекции, от дешёвых к дорогим."""

    async def latest_listings(self, *, limit: int = 20) -> list[Listing]:
        """Самые свежие лоты по всему рынку, от новых к старым.

        Отличается от ``listings`` тем, что не привязана к коллекции: для
        реакции на новое предложение важна не коллекция, а давность. Обход
        коллекций по очереди на это не годится — к своей очереди лот уже
        купят.
        """
        return []

    async def attribute_floors(self, collection: str) -> list[AttributeFloor]:
        """Флоры по моделям, фонам и символам. Не все площадки умеют."""
        return []

    async def activity(
        self, collection: str | None = None, *, limit: int = 100
    ) -> list[ActivityEvent]:
        """Лента событий рынка — источник истории сделок."""
        return []

    async def fetch_asset(self, key: str) -> Any:
        """Файл из хранилища площадки по ключу из ответа API.

        Нужен для цвета модели: он нигде не отдаётся полем, но анимация
        модели лежит в открытом хранилище и содержит цвета явно.
        """
        if not self.endpoints.cdn_url or not key:
            return None

        await self.open()
        assert self._client is not None
        await self._limiter.wait()

        url = f"{self.endpoints.cdn_url.rstrip('/')}/{key.lstrip('/')}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise MarketplaceError(f"{self.name}: не удалось скачать {key}: {exc}") from exc

        if response.status_code == 429:
            self._limiter.penalize()
            raise RateLimited(f"{self.name}: 429 на {key}")
        if response.status_code >= 400:
            raise MarketplaceError(f"{self.name}: {response.status_code} на {key}")

        try:
            return response.json()
        except ValueError as exc:
            raise MarketplaceError(f"{self.name}: {key} не JSON") from exc

    async def my_orders(self) -> list[MarketOrder]:
        """Наши стоящие заявки на покупку."""
        return []

    async def create_order(self, collection: str, price_ton: float, amount: int) -> str:
        raise NotImplementedError(f"{self.name} не поддерживает заявки")

    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError(f"{self.name} не поддерживает заявки")

    async def top_offers(self) -> list[CollectionOffer]:
        """Верхние заявки на покупку по всем коллекциям — одним запросом.

        Это цена гарантированного выхода: сколько дают за подарок прямо
        сейчас, не дожидаясь покупателя. Без неё в балле ликвидности
        отсутствует опора спроса, и остаётся судить только по предложению.
        """
        return []

    # --- Торговля (только там, где supports_trading) ----------------------

    async def balance(self) -> Balance:
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")

    async def inventory(self) -> list[OwnedGift]:
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")

    async def buy(self, listing: Listing) -> str:
        """Покупает лот. Возвращает идентификатор сделки."""
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")

    async def list_for_sale(self, gift_external_id: str, price_ton: float) -> str:
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")

    async def change_price(self, listing_id: str, price_ton: float) -> None:
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")

    async def delist(self, listing_id: str) -> None:
        raise NotImplementedError(f"{self.name} не поддерживает торговлю")
