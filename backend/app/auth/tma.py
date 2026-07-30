"""Получение и обновление токенов площадок.

Telegram выдаёт мини-аппу строку initData, и дальше площадки расходятся:

* **Portals** принимает её напрямую — ``Authorization: tma <initData>``.
* **MRKT** меняет initData на собственный токен через POST /api/v1/auth и
  дальше ждёт его **без всякого префикса**: по записи трафика это UUID из
  36 символов. Дописать «tma» здесь означает сломать авторизацию.

Поэтому схема выбирается по площадке, а не применяется одна на всех.

Два способа получить доступ:

1. **Вручную** — скопировать заголовок Authorization из DevTools. Ничего
   не знает об аккаунте, но раз в несколько дней придётся повторять.
2. **Userbot** — приложение логинится в Telegram через Pyrogram и само
   запрашивает initData у мини-аппа. Работает автономно, но требует
   хранить сессию Telegram локально и повышает риск блокировки аккаунта.

Pyrogram импортируется лениво: без него приложение работает в ручном режиме
и не тянет лишние 20 МБ в exe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.auth.vault import vault
from app.domain import Market

log = logging.getLogger(__name__)

#: Мини-аппы площадок: имя бота и короткое имя приложения для requestAppWebView.
MINI_APPS: dict[Market, tuple[str, str]] = {
    Market.PORTALS: ("portals", "market"),
    Market.MRKT: ("mrkt", "app"),
}

#: Считаем токен протухшим заранее, чтобы не ловить 401 в момент сделки.
REFRESH_MARGIN_SEC = 6 * 3600
DEFAULT_TTL_SEC = 24 * 3600

#: Схемы авторизации различаются, и путать их нельзя.
#:
#: Portals принимает initData Telegram напрямую: ``Authorization: tma <initData>``.
#:
#: MRKT сначала меняет initData на собственный токен через POST /api/v1/auth
#: и дальше ждёт этот токен без всякого префикса — по записи трафика это
#: UUID из 36 символов. Дописывать ему «tma» означает сломать заголовок.
TMA_MARKETS = {Market.PORTALS}

#: Префиксы, при виде которых значение считается готовым заголовком и не
#: дополняется ничем.
KNOWN_SCHEMES = ("tma ", "bearer ", "basic ", "token ")

#: Параметры, по которым узнаётся сырой initData Telegram.
INIT_DATA_MARKERS = ("hash=", "auth_date=")

#: Площадки, у которых учётные данные лежат в cookie.
COOKIE_MARKETS = {Market.GETGEMS}

#: Аналитика в строке cookie. Отправлять её незачем: к авторизации она не
#: относится, зато переносит идентификаторы отслеживания в каждый запрос.
TRACKING_COOKIE_PREFIXES = (
    "_ga",
    "_gid",
    "_gat",
    "_ym_",
    "_fbp",
    "_fbc",
    "_hj",
    "amplitude",
    "mp_",
    "intercom-",
)

#: Имена, по которым видно, что в строке действительно есть авторизация.
AUTH_COOKIE_MARKERS = ("auth_token", "jwt_token", "token", "session")


def clean_cookie_string(value: str) -> str:
    """Убираем из строки cookie всё, что не относится к авторизации.

    Пользователь копирует заголовок целиком, и туда попадает аналитика
    Google и Яндекса. Площадке она не нужна и лишь тащит идентификаторы
    отслеживания в каждый наш запрос.

    Убираем только заведомо известные счётчики: неизвестное имя вполне
    может оказаться сессионным, и терять его нельзя.
    """
    kept: list[str] = []
    for chunk in value.split(";"):
        item = chunk.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip().lower()
        if any(name.startswith(prefix) for prefix in TRACKING_COOKIE_PREFIXES):
            continue
        kept.append(item)
    return "; ".join(kept)


def has_auth_cookie(value: str) -> bool:
    """Есть ли в строке хоть что-то похожее на токен авторизации."""
    lowered = value.lower()
    return any(f"{marker}=" in lowered for marker in AUTH_COOKIE_MARKERS)


def looks_like_init_data(value: str) -> bool:
    """Похоже ли значение на сырой initData Telegram."""
    lowered = value.lower()
    return all(marker in lowered for marker in INIT_DATA_MARKERS)


def normalize_header(market: Market, header_value: str) -> str:
    """Приводим вставленное пользователем значение к готовому заголовку.

    Раньше здесь безусловно дописывался префикс «tma», и это ломало MRKT:
    её токен — обычный UUID без схемы, а «tma <uuid>» площадка отвергает.
    Теперь значение трогается только тогда, когда это действительно
    сырой initData для площадки, которая его и ждёт.
    """
    token = header_value.strip()
    if not token:
        raise ValueError("Пустой токен")

    if market in COOKIE_MARKETS:
        # Учётные данные лежат в cookie: чистим от аналитики и отдаём.
        cleaned = clean_cookie_string(token)
        if not cleaned:
            raise ValueError("В строке cookie не осталось ничего, кроме аналитики")
        return cleaned

    lowered = token.lower()
    if any(lowered.startswith(scheme) for scheme in KNOWN_SCHEMES):
        return token  # схема уже указана — не вмешиваемся

    if market in TMA_MARKETS and looks_like_init_data(token):
        return f"tma {token}"

    # Всё остальное — собственный токен площадки, отдаём как есть.
    return token


@dataclass(slots=True)
class TokenState:
    value: str | None
    obtained_at: float
    source: str  # "manual" | "userbot"

    @property
    def age_sec(self) -> float:
        return time.time() - self.obtained_at

    @property
    def looks_stale(self) -> bool:
        return self.value is None or self.age_sec > DEFAULT_TTL_SEC - REFRESH_MARGIN_SEC


class TmaAuth:
    """Держит токены площадок и умеет их обновлять."""

    def __init__(self) -> None:
        self._tokens: dict[Market, TokenState] = {}
        self._load_from_vault()

    def _load_from_vault(self) -> None:
        for market in Market:
            stored = vault.get(f"tma:{market.value}")
            if stored:
                obtained = vault.get(f"tma_ts:{market.value}")
                self._tokens[market] = TokenState(
                    value=stored,
                    obtained_at=float(obtained) if obtained else 0.0,
                    source=vault.get(f"tma_src:{market.value}") or "manual",
                )

    # --- Ручной режим ----------------------------------------------------

    def set_manual(self, market: Market, header_value: str) -> None:
        """Сохраняем токен, вставленный пользователем из DevTools."""
        token = normalize_header(market, header_value)
        state = TokenState(value=token, obtained_at=time.time(), source="manual")
        self._tokens[market] = state
        vault.set(f"tma:{market.value}", token)
        vault.set(f"tma_ts:{market.value}", str(state.obtained_at))
        vault.set(f"tma_src:{market.value}", "manual")
        log.info("Токен %s сохранён (вручную)", market.value)

    def get(self, market: Market) -> str | None:
        state = self._tokens.get(market)
        return state.value if state else None

    def state(self, market: Market) -> TokenState | None:
        return self._tokens.get(market)

    def forget(self, market: Market) -> None:
        self._tokens.pop(market, None)
        for prefix in ("tma", "tma_ts", "tma_src"):
            vault.delete(f"{prefix}:{market.value}")

    # --- Userbot ---------------------------------------------------------

    @staticmethod
    def userbot_available() -> bool:
        try:
            import pyrogram  # noqa: F401
        except ImportError:
            return False
        return True

    def save_credentials(self, api_id: str, api_hash: str) -> None:
        if not api_id.strip().isdigit():
            raise ValueError("api_id должен быть числом (получить на my.telegram.org)")
        vault.set("tg_api_id", api_id.strip())
        vault.set("tg_api_hash", api_hash.strip())

    def has_credentials(self) -> bool:
        return vault.has("tg_api_id") and vault.has("tg_api_hash")

    async def refresh_via_userbot(self, market: Market) -> str:
        """Запрашиваем свежий initData у мини-аппа через Telegram-сессию.

        Требует ранее выполненного входа: файл сессии Pyrogram лежит в
        папке данных приложения.
        """
        if market not in MINI_APPS:
            raise ValueError(f"{market.value}: обновление токена не поддерживается")
        if not self.has_credentials():
            raise RuntimeError("Не заданы api_id и api_hash — заполните их в Настройках")

        try:
            from pyrogram import Client
            from pyrogram.raw.functions.messages import RequestAppWebView
            from pyrogram.raw.types import InputBotAppShortName
        except ImportError as exc:
            raise RuntimeError(
                "Pyrogram не установлен. Установите: pip install pyrogram. "
                "TgCrypto ставить не нужно: он только ускоряет шифрование, "
                "колёс под свежий Python у него нет, и без компилятора "
                "установка падает."
            ) from exc

        from app import paths

        bot_username, app_short_name = MINI_APPS[market]
        api_id = int(vault.get("tg_api_id") or 0)
        api_hash = vault.get("tg_api_hash") or ""

        async with Client(
            name="flipper",
            api_id=api_id,
            api_hash=api_hash,
            workdir=str(paths.data_dir()),
        ) as client:
            peer = await client.resolve_peer(bot_username)
            result = await client.invoke(
                RequestAppWebView(
                    peer=peer,
                    app=InputBotAppShortName(bot_id=peer, short_name=app_short_name),
                    platform="web",
                    write_allowed=True,
                )
            )
            init_data = _init_data_from_url(result.url)

        token = await self._init_data_to_header(market, init_data)
        state = TokenState(value=token, obtained_at=time.time(), source="userbot")
        self._tokens[market] = state
        vault.set(f"tma:{market.value}", token)
        vault.set(f"tma_ts:{market.value}", str(state.obtained_at))
        vault.set(f"tma_src:{market.value}", "userbot")
        log.info("Токен %s обновлён через userbot", market.value)
        return token

    @staticmethod
    async def _init_data_to_header(market: Market, init_data: str) -> str:
        """Превращаем initData в готовый заголовок для конкретной площадки.

        Portals принимает initData как есть. MRKT требует предварительного
        обмена на собственный токен — без него авторизация не пройдёт,
        сколько ни приписывай префиксов.
        """
        if market in TMA_MARKETS:
            return f"tma {init_data}"

        if market is Market.MRKT:
            # Импорт внутри функции: реестр адаптеров сам зависит от этого
            # модуля, и на верхнем уровне вышел бы цикл.
            from app.adapters.mrkt import DEFAULT_ENDPOINTS, MrktAdapter

            adapter = MrktAdapter(endpoints=DEFAULT_ENDPOINTS, fee_sell=0.0)
            async with adapter:
                return await adapter.exchange_init_data(init_data)

        return init_data

    async def ensure_fresh(self, market: Market) -> str | None:
        """Обновляем токен, если он выдохся и доступен userbot-режим."""
        state = self._tokens.get(market)
        if state and not state.looks_stale:
            return state.value

        if state and state.source == "userbot" and self.has_credentials():
            try:
                return await self.refresh_via_userbot(market)
            except Exception:  # noqa: BLE001 — падать на обновлении нельзя
                log.exception("Не удалось обновить токен %s", market.value)

        return state.value if state else None


def _init_data_from_url(url: str) -> str:
    """initData лежит во фрагменте ссылки мини-аппа: ...#tgWebAppData=<...>."""
    from urllib.parse import parse_qs, unquote, urlparse

    fragment = urlparse(url).fragment
    values = parse_qs(fragment).get("tgWebAppData")
    if not values:
        raise RuntimeError("Telegram не вернул tgWebAppData — не удалось получить токен")
    return unquote(values[0])


auth = TmaAuth()
