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

Pyrogram импортируется лениво — на старте он не нужен, — но в сборку
входит. Отдельная установка через pip на состояние приложения не влияет:
exe несёт своё окружение и системный site-packages не видит, поэтому
надпись «Pyrogram не установлен» от неё и не исчезала.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from app.auth.vault import vault
from app.domain import Market

log = logging.getLogger(__name__)

#: Мини-аппы площадок: имя бота и короткое имя приложения для requestAppWebView.
MINI_APPS: dict[Market, tuple[str, str]] = {
    Market.PORTALS: ("portals", "market"),
    Market.MRKT: ("mrkt", "app"),
    Market.TONNEL: ("tonnel_network_bot", "gifts"),
}

#: Площадки, которым initData уходит в теле запроса как есть, без схемы.
BODY_AUTH_MARKETS = {Market.TONNEL}

#: Считаем токен протухшим заранее, чтобы не ловить 401 в момент сделки.
REFRESH_MARGIN_SEC = 6 * 3600
DEFAULT_TTL_SEC = 24 * 3600

#: Имя файла сессии Telegram в папке данных.
SESSION_NAME = "flipper"

#: Сколько ждать ответа Telegram на шаге входа. Своими средствами
#: Pyrogram повторяет подключение бесконечно, и запрос из интерфейса
#: висел бы вечно — вместо внятного «Telegram недоступен».
LOGIN_TIMEOUT_SEC = 25.0

#: Сколько раз пробовать удалить файл сессии. Windows держит его
#: заблокированным, пока клиент не отпустит, а отпускает он не мгновенно.
UNLINK_ATTEMPTS = 5

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


def parse_proxy(url: str) -> dict | None:
    """Разбираем адрес прокси в вид, который ждёт Pyrogram.

    Формат обычный: ``socks5://логин:пароль@хост:порт``. Логин и пароль
    необязательны.
    """
    value = (url or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("socks5", "socks4", "http"):
        raise ValueError("Поддерживаются схемы socks5, socks4 и http")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Укажите адрес и порт: socks5://хост:порт")

    proxy = {"scheme": scheme, "hostname": parsed.hostname, "port": int(parsed.port)}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


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
    if market in BODY_AUTH_MARKETS:
        # Значение уходит в тело запроса, где схемы не бывает. Префикс
        # «tma» тут не безобиден: с ним подпись не сойдётся, а площадка
        # ответит невнятной ошибкой. Пользователь же копирует initData по
        # привычке от Portals — вместе с префиксом.
        if lowered.startswith("tma "):
            return token[4:].strip()
        return token

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


@dataclass
class PendingLogin:
    """Незавершённый вход в Telegram: клиент живёт между шагами."""

    client: object
    phone: str
    code_hash: str
    awaiting_password: bool = False


async def _quietly_disconnect(client) -> None:
    """Отключаемся, не мешая исходной ошибке всплыть."""
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001 — при разборе завала это уже не важно
        log.debug("Клиент Telegram не отключился штатно", exc_info=True)


class TmaAuth:
    """Держит токены площадок и умеет их обновлять."""

    def __init__(self) -> None:
        self._tokens: dict[Market, TokenState] = {}
        self._pending: PendingLogin | None = None
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

    # --- Вход в Telegram -------------------------------------------------
    #
    # Вход разбит на шаги намеренно. Pyrogram умеет спросить телефон и код
    # сам, но спрашивает он их через stdin: в приложении с веб-интерфейсом
    # это означало бы зависший на input() сервер и наглухо замерший
    # интерфейс. Поэтому каждый шаг — отдельный запрос, а незавершённый
    # клиент живёт между ними здесь.

    def session_path(self):
        from app import paths

        return paths.data_dir() / f"{SESSION_NAME}.session"

    def has_session(self) -> bool:
        """Выполнен ли вход в Telegram."""
        return self.session_path().exists()

    def save_proxy(self, url: str) -> None:
        """Прокси для подключения к Telegram.

        На части сетей Telegram недоступен напрямую, и без прокси вход не
        состоится вовсе: Pyrogram будет бесконечно повторять подключение.
        """
        value = url.strip()
        if value:
            parse_proxy(value)  # проверяем формат до сохранения
            vault.set("tg_proxy", value)
        else:
            vault.delete("tg_proxy")

    def proxy_url(self) -> str:
        return vault.get("tg_proxy") or ""

    def _new_client(self, *, ipv6: bool = False):
        from pyrogram import Client

        from app import paths

        return Client(
            name=SESSION_NAME,
            api_id=int(vault.get("tg_api_id") or 0),
            api_hash=vault.get("tg_api_hash") or "",
            workdir=str(paths.data_dir()),
            proxy=parse_proxy(self.proxy_url()),
            ipv6=ipv6,
        )

    async def _connect_any(self):
        """Подключаемся к Telegram, пробуя оба стека адресов.

        Pyrogram ходит не по домену, а на зашитые адреса дата-центров
        (например 149.154.167.51) обычным TCP на порт 443. Когда провайдер
        режет Telegram, режет он обычно диапазоны IPv4 — списки блокировок
        до IPv6 доходят реже. Поэтому при неудаче по IPv4 пробуем IPv6:
        это бесплатно и на части сетей снимает нужду в прокси.
        """
        errors: list[str] = []
        for ipv6 in (False, True):
            client = self._new_client(ipv6=ipv6)
            try:
                await asyncio.wait_for(client.connect(), timeout=LOGIN_TIMEOUT_SEC)
            except (TimeoutError, OSError, ConnectionError) as exc:
                errors.append(f"IPv{6 if ipv6 else 4}: {exc or 'нет ответа'}")
                await _quietly_disconnect(client)
                continue
            if ipv6:
                log.info("К Telegram удалось подключиться только по IPv6")
            return client

        raise RuntimeError(
            "Telegram не отвечает ни по IPv4, ни по IPv6 — почти наверняка "
            "его режет провайдер. Укажите прокси в поле ниже "
            "(socks5://хост:порт) либо пользуйтесь ручным вводом токена: "
            "на сбор данных и торговлю это никак не влияет, там обычный "
            "HTTPS к площадке. Подробности: " + "; ".join(errors)
        )

    async def send_message(self, chat: str, text: str) -> None:
        """Отправляем сообщение своей же сессией Telegram.

        Отдельный бот тут был бы лишней сущностью: пришлось бы заводить
        токен, находить chat_id и держать ещё одно подключение. Сессия уже
        есть — ею и пользуемся, а ``me`` означает «Избранное».
        """
        if not self.has_session():
            raise RuntimeError(
                "Вход в Telegram не выполнен — уведомления отправлять некому"
            )
        if not self.userbot_available():
            raise RuntimeError("Pyrogram недоступен в этой сборке")

        client = await self._connect_any()
        try:
            await asyncio.wait_for(
                client.send_message(chat or "me", text), timeout=LOGIN_TIMEOUT_SEC
            )
        finally:
            await _quietly_disconnect(client)

    async def begin_login(self, phone: str) -> str:
        """Шаг первый: просим Telegram прислать код."""
        if not self.has_credentials():
            raise RuntimeError("Сначала задайте api_id и api_hash")

        await self.cancel_login()
        client = await self._connect_any()
        try:
            sent = await asyncio.wait_for(
                client.send_code(phone.strip()), timeout=LOGIN_TIMEOUT_SEC
            )
        except Exception:
            await _quietly_disconnect(client)
            raise

        self._pending = PendingLogin(
            client=client, phone=phone.strip(), code_hash=sent.phone_code_hash
        )
        # Ни телефон, ни код в журнал не пишем.
        return "Код отправлен в Telegram"

    async def complete_login(self, code: str) -> str:
        """Шаг второй: подтверждаем код. Может потребоваться пароль."""
        from pyrogram.errors import SessionPasswordNeeded

        pending = self._require_pending()
        try:
            await asyncio.wait_for(
                pending.client.sign_in(pending.phone, pending.code_hash, code.strip()),
                timeout=LOGIN_TIMEOUT_SEC,
            )
        except SessionPasswordNeeded:
            pending.awaiting_password = True
            return "needs_password"
        except Exception:
            await self.cancel_login()
            raise

        await self._finish_login()
        return "ok"

    async def complete_password(self, password: str) -> str:
        """Шаг третий: двухфакторный пароль, если он включён."""
        pending = self._require_pending()
        try:
            await asyncio.wait_for(
                pending.client.check_password(password), timeout=LOGIN_TIMEOUT_SEC
            )
        except Exception:
            await self.cancel_login()
            raise

        await self._finish_login()
        return "ok"

    async def cancel_login(self) -> None:
        if self._pending is not None:
            await _quietly_disconnect(self._pending.client)
            self._pending = None

    async def forget_session(self) -> str:
        """Удаляем сессию Telegram вместе с файлом.

        Файл держит открытым сам клиент, а Windows не даёт удалить занятый
        файл. Поэтому сначала отпускаем клиента и только потом удаляем, с
        несколькими попытками: отпускает он не мгновенно.
        """
        await self.cancel_login()
        path = self.session_path()
        if not path.exists():
            return "Сессии и не было"

        for attempt in range(UNLINK_ATTEMPTS):
            try:
                path.unlink()
            except PermissionError:
                await asyncio.sleep(0.3 * (attempt + 1))
            except OSError as exc:
                return f"Не удалось удалить файл сессии: {exc}"
            else:
                return "Сессия удалена"

        return (
            "Файл сессии занят подключением к Telegram. Перезапустите "
            "приложение и повторите — при старте он не открыт."
        )

    def _require_pending(self) -> PendingLogin:
        if self._pending is None:
            raise RuntimeError("Вход не начат — сначала запросите код")
        return self._pending

    async def _finish_login(self) -> None:
        """Сохраняем сессию на диск и отпускаем клиента."""
        pending = self._require_pending()
        try:
            await pending.client.storage.save()
        finally:
            await _quietly_disconnect(pending.client)
            self._pending = None
        log.info("Вход в Telegram выполнен, сессия сохранена")

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
                "Pyrogram недоступен в этой сборке. Устанавливать его "
                "отдельно бесполезно: exe несёт своё окружение и системный "
                "site-packages не видит."
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

    async def ensure_fresh(self, market: Market, *, rejected: str | None = None) -> str | None:
        """Свежий токен площадки или None, если взять его неоткуда.

        ``rejected`` — токен, который площадка только что отвергла. Вернуть
        его же в ответ означало бы соврать: вызывающий спрашивает именно
        потому, что этот токен не работает. Ровно так и получалось —
        сканер принимал отказ за успешное обновление, повторял запрос,
        снова получал 401, и цикл крутился бесконечно, ни разу не сообщив
        пользователю, что нужен новый токен.

        Обновляем и вручную заданный токен тоже. Раньше условием стоял
        источник «userbot», из-за чего протухший ручной токен не пытались
        обновить, даже когда учётные данные Telegram были заданы.
        """
        state = self._tokens.get(market)
        current = state.value if state else None
        usable = bool(current) and current != rejected

        if usable and state is not None and not state.looks_stale:
            return current

        if self.has_credentials() and market in MINI_APPS:
            try:
                return await self.refresh_via_userbot(market)
            except Exception:  # noqa: BLE001 — падать на обновлении нельзя
                log.exception("Не удалось обновить токен %s", market.value)

        return current if usable else None

    async def verify(self, market: Market) -> str:
        """Проверяем токен одним запросом. Пустая строка — всё в порядке.

        Без проверки пользователь узнаёт о негодном токене только из
        журнала, через минуты бесплодных попыток. Один запрос сразу после
        сохранения отвечает на вопрос «работает ли то, что я вставил».
        """
        from app.adapters import registry
        from app.adapters.base import AuthExpired, MarketplaceError
        from app.config import settings

        adapter = registry.build_adapter(market, settings)
        probe = "me" if "me" in adapter.endpoints.endpoints else "balance"
        if probe not in adapter.endpoints.endpoints:
            return ""

        try:
            async with adapter:
                await adapter.request(probe)
        except AuthExpired:
            return "площадка отвергла токен — вероятно, он уже протух"
        except MarketplaceError as exc:
            # Сеть или сама площадка легли: это не приговор токену.
            return f"проверить не удалось: {exc}"
        return ""


def _init_data_from_url(url: str) -> str:
    """initData лежит во фрагменте ссылки мини-аппа: ...#tgWebAppData=<...>."""
    from urllib.parse import parse_qs, unquote, urlparse

    fragment = urlparse(url).fragment
    values = parse_qs(fragment).get("tgWebAppData")
    if not values:
        raise RuntimeError("Telegram не вернул tgWebAppData — не удалось получить токен")
    return unquote(values[0])


auth = TmaAuth()
