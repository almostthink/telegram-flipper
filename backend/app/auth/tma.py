"""Получение и обновление tma-токена площадок.

Мини-аппы Telegram авторизуют запросы заголовком ``tma <initData>``.
initData выдаёт сам Telegram при открытии мини-аппа и живёт 1-7 дней.

Два способа его получить:

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
        token = header_value.strip()
        if not token:
            raise ValueError("Пустой токен")
        if not token.lower().startswith("tma "):
            # Пользователь мог скопировать только initData без префикса.
            token = f"tma {token}"

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
                "Pyrogram не установлен. Установите: pip install pyrogram tgcrypto"
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

        token = f"tma {init_data}"
        state = TokenState(value=token, obtained_at=time.time(), source="userbot")
        self._tokens[market] = state
        vault.set(f"tma:{market.value}", token)
        vault.set(f"tma_ts:{market.value}", str(state.obtained_at))
        vault.set(f"tma_src:{market.value}", "userbot")
        log.info("Токен %s обновлён через userbot", market.value)
        return token

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
