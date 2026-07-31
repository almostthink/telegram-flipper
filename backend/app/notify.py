"""Уведомления в Telegram.

Приложение живёт на компьютере, а решения по подарку иногда нужно
принимать не сидя перед ним. Главное такое решение — переносить ли лот на
другую площадку: перенос делается руками, через ЛС бота, и стоит звёзд.
Поэтому в сообщении о покупке сразу идут цены коллекции по всем площадкам:
без них решение принимать не из чего.

Два правила, которые здесь важнее удобства:

**Уведомление никогда не мешает торговле.** Отправка идёт отдельной
задачей, её ошибки не поднимаются в торговый цикл, а Telegram может быть
недоступен неделями — на сделки это не влияет никак.

**Сообщение не содержит секретов.** Ни токенов, ни initData, ни номера
телефона: в «Избранном» они хранились бы вечно.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings

log = logging.getLogger(__name__)

#: Сколько сообщений держим в памяти как отправленные. Нужно, чтобы
#: разница цен между площадками не повторялась каждым циклом: она держится
#: часами, а сообщение о ней осмысленно один раз.
SEEN_LIMIT = 500


class Notifier:
    """Отправка сообщений в Telegram с защитой от повторов."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error: str = ""
        self._seen: list[str] = []
        #: Ссылки на задачи: без них сборщик мусора вправе убить отправку
        #: на полпути, и сообщение молча не уйдёт.
        self._tasks: set[asyncio.Task] = set()

    # --- Отправка --------------------------------------------------------

    def post(self, text: str, *, key: str = "") -> None:
        """Поставить сообщение в очередь и сразу вернуться.

        Торговый цикл не должен ждать Telegram: подключение занимает
        секунды, а в быстрой петле секунды и есть всё преимущество.
        """
        if not self.settings.notify.enabled or not text.strip():
            return
        if key and not self._remember(key):
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Синхронный контекст (тесты, скрипты) — отправлять нечем.
            return

        task = loop.create_task(self._send_quietly(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, text: str) -> None:
        """Отправка с ожиданием — для кнопки «проверить» в настройках."""
        from app.auth.tma import auth

        await auth.send_message(self.settings.notify.chat, text)

    async def _send_quietly(self, text: str) -> None:
        try:
            await self.send(text)
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
        self.post("\n".join(lines))

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

    def spread(self, *, collection: str, market: str, floors: dict[str, float]) -> None:
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
        self.post("\n".join(lines), key=f"spread:{collection}:{market}:{best}")

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
                f"На {best} дороже. Хотите продать там — нажмите «Заморозить» "
                f"в приложении и перенесите подарок сами: перенос стоит "
                f"{stars} ⭐ (~{cost:.2f} TON). Без заморозки лот уйдёт "
                f"на {market}, где куплен."
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
