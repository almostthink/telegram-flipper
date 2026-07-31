"""Уведомления в Telegram через бота — с кнопками.

Приложение живёт на компьютере, а решения по подарку приходится принимать
не сидя перед ним. Главное такое решение — переносить ли лот на другую
площадку: перенос делается руками, через ЛС бота площадки, и стоит звёзд.
Поэтому в сообщении о покупке сразу идут цены коллекции по всем площадкам,
а рядом — кнопка «Заморозить»: нажал в телефоне, и движок подарок не
тронет, пока вы его переносите.

Почему бот, а не своя сессия. Кнопку под сообщением может повесить только
бот: у обычного аккаунта inline-клавиатур не бывает. Заодно это снимает
риск — сессия аккаунта даёт доступ ко всей переписке, токен бота не даёт
ничего, кроме этого самого бота.

Почему long polling, а не webhook: у приложения нет и не должно быть
публичного адреса. Оно слушает только 127.0.0.1, а за обновлениями ходит
само.

Два правила, которые здесь важнее удобства:

**Уведомление никогда не мешает торговле.** Отправка идёт отдельной
задачей, её ошибки в торговый цикл не поднимаются, а Telegram может быть
недоступен неделями — на сделки это не влияет.

**Команды принимаются только из своего чата.** Иначе любой, кто найдёт
бота, сможет замораживать чужие позиции.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from app.auth.vault import vault
from app.config import Settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

#: Ключ токена в хранилище секретов. В config.json ему не место.
TOKEN_KEY = "tg_bot_token"

#: Сколько секунд держим соединение getUpdates открытым. Дольше — риск
#: словить разрыв на прокси, короче — лишние запросы на пустом месте.
POLL_TIMEOUT_SEC = 25

#: Пауза после сбоя опроса. Telegram недоступен — не повод молотить сеть.
RETRY_DELAY_SEC = 5.0

#: Сколько сообщений помним как отправленные, чтобы не повторять одно и
#: то же: разница цен держится часами, а сообщение о ней осмысленно раз.
SEEN_LIMIT = 500


def bot_token() -> str:
    return (vault.get(TOKEN_KEY) or "").strip()


def save_bot_token(token: str) -> None:
    value = token.strip()
    if not value:
        vault.delete(TOKEN_KEY)
        return
    if ":" not in value:
        raise ValueError(
            "Не похоже на токен бота. Он выглядит как 123456789:AAE... — "
            "его выдаёт @BotFather сразу после /newbot"
        )
    vault.set(TOKEN_KEY, value)


class BotApi:
    """Тонкая обёртка над Bot API. Ошибки отдаёт текстом, а не молчанием."""

    def __init__(self, token: str) -> None:
        self.token = token

    async def call(self, method: str, payload: dict[str, Any], *, timeout: float = 20.0):
        url = f"{API_ROOT}/bot{self.token}/{method}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)

        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Telegram ответил не по-человечески: {response.text[:200]}"
            ) from exc

        if not body.get("ok"):
            raise RuntimeError(body.get("description") or f"Telegram: HTTP {response.status_code}")
        return body.get("result")

    async def me(self) -> dict:
        return await self.call("getMe", {})

    async def send(self, chat_id: int | str, text: str, buttons: list[list[dict]] | None = None):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def edit_markup(
        self, chat_id: int, message_id: int, buttons: list[list[dict]] | None
    ) -> None:
        await self.call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": buttons or []},
            },
        )

    async def updates(self, offset: int) -> list[dict]:
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": POLL_TIMEOUT_SEC,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=POLL_TIMEOUT_SEC + 10,
        )


def freeze_button(position_id: int, *, frozen: bool) -> list[list[dict]]:
    """Кнопка под сообщением. Надпись всегда о том, что произойдёт."""
    if frozen:
        return [[{"text": "Разморозить", "callback_data": f"unfreeze:{position_id}"}]]
    return [[{"text": "❄️ Заморозить", "callback_data": f"freeze:{position_id}"}]]


class Notifier:
    """Сборка сообщений и отправка их ботом."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error: str = ""
        self._seen: list[str] = []
        #: Ссылки на задачи: без них сборщик мусора вправе убить отправку
        #: на полпути, и сообщение молча не уйдёт.
        self._tasks: set[asyncio.Task] = set()

    # --- Состояние -------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Есть и токен, и привязанный чат — иначе слать некому."""
        return bool(bot_token()) and bool(self.settings.notify.chat_id)

    def api(self) -> BotApi:
        token = bot_token()
        if not token:
            raise RuntimeError("Токен бота не задан — вставьте его в Настройках")
        return BotApi(token)

    # --- Отправка --------------------------------------------------------

    def post(self, text: str, *, key: str = "", buttons=None) -> None:
        """Поставить сообщение в очередь и сразу вернуться.

        Торговый цикл не должен ждать Telegram: подключение занимает
        секунды, а в быстрой петле секунды и есть всё преимущество.
        """
        if not self.settings.notify.enabled or not text.strip() or not self.ready:
            return
        if key and not self._remember(key):
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Синхронный контекст (тесты, скрипты) — отправлять нечем.
            return

        task = loop.create_task(self._send_quietly(text, buttons))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, text: str, buttons=None) -> None:
        """Отправка с ожиданием — для кнопки «Проверить» в настройках."""
        chat_id = self.settings.notify.chat_id
        if not chat_id:
            raise RuntimeError(
                "Чат не привязан — откройте бота в Telegram и отправьте ему /start"
            )
        await self.api().send(chat_id, text, buttons)

    async def _send_quietly(self, text: str, buttons=None) -> None:
        try:
            await self.send(text, buttons)
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 — уведомления не роняют торговлю
            self.last_error = str(exc) or exc.__class__.__name__
            log.debug("Уведомление не отправлено: %s", self.last_error)

    def _remember(self, key: str) -> bool:
        """False, если про это уже сообщали."""
        if key in self._seen:
            return False
        self._seen.append(key)
        if len(self._seen) > SEEN_LIMIT:
            del self._seen[: len(self._seen) - SEEN_LIMIT]
        return True

    # --- Сообщения -------------------------------------------------------

    def buy(
        self,
        *,
        collection: str,
        model: str | None,
        number: int | None,
        market: str,
        price_ton: float,
        fair_ton: float | None,
        roi: float | None,
        floors: dict[str, float],
        paper: bool,
        position_id: int | None = None,
    ) -> None:
        if not self.settings.notify.on_buy:
            return

        title = "Куплено" if not paper else "Куплено (PAPER)"
        name = collection
        if number:
            name += f" #{number}"
        if model:
            name += f" · {model}"

        lines = [f"{title} · {market}", name, f"Цена {price_ton:.2f} TON"]
        if fair_ton:
            tail = f"Справедливая {fair_ton:.2f} TON"
            if roi is not None:
                tail += f" · ROI {roi * 100:+.0f}%"
            lines.append(tail)

        lines.extend(self.price_lines(market, floors))
        buttons = freeze_button(position_id, frozen=False) if position_id else None
        self.post("\n".join(lines), buttons=buttons)

    def sold(
        self,
        *,
        collection: str,
        market: str,
        buy_ton: float,
        sell_ton: float,
        pnl_ton: float | None,
        paper: bool,
    ) -> None:
        if not self.settings.notify.on_sell:
            return

        title = "Продано" if not paper else "Продано (PAPER)"
        lines = [
            f"{title} · {market}",
            collection,
            f"Куплено {buy_ton:.2f} → продано {sell_ton:.2f} TON",
        ]
        if pnl_ton is not None:
            lines.append(f"Результат {pnl_ton:+.2f} TON")
        self.post("\n".join(lines))

    def spread(
        self,
        *,
        collection: str,
        market: str,
        floors: dict[str, float],
        position_id: int | None = None,
    ) -> None:
        """Сообщаем, что позицию выгоднее продавать на другой площадке.

        Ключ повтора — по коллекции и площадке: разница держится часами, и
        напоминать о ней каждые двадцать минут значит превратить
        уведомления в шум, который перестают читать.
        """
        best = best_market(market, floors, self.settings.notify.min_spread)
        if best is None:
            return

        lines = [f"Разница площадок · {collection}"]
        lines.extend(self.price_lines(market, floors))
        buttons = freeze_button(position_id, frozen=False) if position_id else None
        self.post(
            "\n".join(lines),
            key=f"spread:{collection}:{market}:{best}",
            buttons=buttons,
        )

    def price_lines(self, market: str, floors: dict[str, float]) -> list[str]:
        """Цены по площадкам плюс подсказка про перенос, если он окупается."""
        if not floors:
            return []

        own = floors.get(market)
        lines = ["", "Цена коллекции:"]
        for name, floor in sorted(floors.items(), key=lambda item: item[1]):
            mark = " ←" if name == market else ""
            gap = ""
            if own and own > 0 and name != market:
                gap = f"  ({(floor / own - 1) * 100:+.0f}%)"
            lines.append(f"  {name}: {floor:.2f} TON{gap}{mark}")

        best = best_market(market, floors, self.settings.notify.min_spread)
        if best is not None:
            cost = self.settings.transfer.cost_ton
            stars = self.settings.transfer.stars_per_gift
            lines.append("")
            lines.append(
                f"На {best} дороже. Хотите продать там — жмите «Заморозить» и "
                f"переносите подарок сами: перенос стоит {stars} ⭐ "
                f"(~{cost:.2f} TON). Без заморозки лот уйдёт на {market}, "
                f"где куплен."
            )
        return lines


def best_market(market: str, floors: dict[str, float], min_spread: float) -> str | None:
    """Площадка, где заметно дороже, чем на своей. None — переносить незачем.

    Сравнение с порогом обязательно: разница в пару процентов не окупает
    ни звёзд за перенос, ни времени на него, а уведомление о ней — шум.
    """
    own = floors.get(market)
    if not own or own <= 0:
        return None

    candidates = {
        name: floor
        for name, floor in floors.items()
        if name != market and floor > own * (1 + min_spread)
    }
    if not candidates:
        return None
    return max(candidates, key=lambda name: candidates[name])


# --- Приём нажатий --------------------------------------------------------


def parse_callback(data: str) -> tuple[str, int] | None:
    """Разбираем callback_data вида ``freeze:12``. None — чужой формат."""
    action, _, raw = (data or "").partition(":")
    if action not in ("freeze", "unfreeze") or not raw.isdigit():
        return None
    return action, int(raw)


class BotPoller:
    """Фоновый опрос Telegram: команды и нажатия кнопок.

    Опрашивает сам, потому что webhook требует публичного адреса, а
    приложение слушает только 127.0.0.1 и остаётся за NAT.
    """

    def __init__(self, settings: Settings, engine) -> None:
        self.settings = settings
        self.engine = engine
        self.offset = 0
        self.last_error: str = ""
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not bot_token():
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())
        log.info("Бот уведомлений запущен")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def _loop(self) -> None:
        while True:
            try:
                updates = await BotApi(bot_token()).updates(self.offset)
                self.last_error = ""
                for update in updates or []:
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    await self._handle(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — опрос не должен умирать
                self.last_error = str(exc) or exc.__class__.__name__
                log.debug("Опрос Telegram не удался: %s", self.last_error)
                await asyncio.sleep(RETRY_DELAY_SEC)

    # --- Обработка -------------------------------------------------------

    async def _handle(self, update: dict) -> None:
        if "message" in update:
            await self._handle_message(update["message"])
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])

    async def _handle_message(self, message: dict) -> None:
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        text = (message.get("text") or "").strip()
        if not chat_id or not text.startswith("/start"):
            return

        bound = self.settings.notify.chat_id
        if bound and bound != chat_id:
            # Чужой чат. Молча игнорируем: отвечать — значит подтверждать
            # постороннему, что бот живой и чем-то управляет.
            log.info("Команда из непривязанного чата отклонена")
            return

        if not bound:
            self.settings.notify.chat_id = chat_id
            self.settings.save()
            log.info("Чат для уведомлений привязан")

        await BotApi(bot_token()).send(
            chat_id,
            "Готово. Сюда будут приходить уведомления о покупках и о разнице "
            "цен между площадками.\n\n"
            "Под сообщением о покупке будет кнопка «Заморозить»: она "
            "останавливает движок по этой позиции, чтобы вы успели перенести "
            "подарок на другую площадку вручную. Без заморозки лот продаётся "
            "там, где куплен.",
        )

    async def _handle_callback(self, query: dict) -> None:
        api = BotApi(bot_token())
        callback_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or 0)

        if self.settings.notify.chat_id and chat_id != self.settings.notify.chat_id:
            await api.answer_callback(callback_id, "Этот чат не привязан к приложению")
            return

        parsed = parse_callback(str(query.get("data") or ""))
        if parsed is None:
            await api.answer_callback(callback_id, "Непонятная команда")
            return

        action, position_id = parsed
        frozen = action == "freeze"
        ok, detail = await self.engine.set_frozen(position_id, frozen)

        await api.answer_callback(callback_id, detail if ok else f"Не вышло: {detail}")
        if not ok:
            return

        try:
            await api.edit_markup(
                chat_id,
                int(message.get("message_id") or 0),
                freeze_button(position_id, frozen=frozen),
            )
        except Exception:  # noqa: BLE001 — состояние уже изменено, кнопка вторична
            log.debug("Не удалось обновить кнопку", exc_info=True)
