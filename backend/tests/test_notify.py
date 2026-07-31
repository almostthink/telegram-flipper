"""Проверки уведомлений и заморозки позиции.

Перенос подарка между площадками приложение не делает — это ручная
операция через ЛС бота, за звёзды. Значит, вся его роль здесь сводится к
двум вещам: вовремя сказать, что на другой площадке дороже, и не продать
подарок, пока человек его переносит.

Обе вещи ломаются молча, поэтому и проверяются: незамеченная разница цен
означает недополученную прибыль, а проданный из-под рук подарок — сделку,
которой человек не хотел.
"""

from __future__ import annotations

import pytest
from app.config import MarketplaceConfig, Settings
from app.notify import Notifier, best_market, freeze_button, parse_callback
from app.storage.db import init_db, session_scope
from app.storage.models import Position
from app.trading.engine import TradingEngine


@pytest.fixture
async def db():
    await init_db()
    yield


def make_settings(*, paper: bool = True) -> Settings:
    settings = Settings(paper_mode=paper)
    settings.marketplaces = {
        "mrkt": MarketplaceConfig(enabled=True, trade_enabled=True),
        "tonnel": MarketplaceConfig(enabled=True, trade_enabled=True),
    }
    return settings


class Recorder(Notifier):
    """Уведомитель, который никуда не ходит — только запоминает сообщения."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.sent: list[str] = []
        self.buttons: list[list[list[dict]] | None] = []

    @property
    def ready(self) -> bool:
        return True

    def post(self, text: str, *, key: str = "", buttons=None) -> None:
        if not self.settings.notify.enabled or not text.strip():
            return
        if key and not self._remember(key):
            return
        self.sent.append(text)
        self.buttons.append(buttons)


# --- Выбор площадки -------------------------------------------------------


def test_higher_market_is_found():
    best = best_market("tonnel", {"tonnel": 10.0, "mrkt": 12.0}, min_spread=0.05)
    assert best == "mrkt"


def test_small_difference_is_not_worth_a_message():
    """Пара процентов не окупает ни звёзд за перенос, ни времени на него."""
    assert best_market("tonnel", {"tonnel": 10.0, "mrkt": 10.2}, min_spread=0.05) is None


def test_cheaper_market_is_never_suggested():
    assert best_market("mrkt", {"mrkt": 12.0, "tonnel": 9.0}, min_spread=0.05) is None


def test_unknown_own_price_gives_no_advice():
    """Без своей цены сравнивать не с чем — и советовать нечего."""
    assert best_market("mrkt", {"tonnel": 9.0}, min_spread=0.05) is None


def test_the_dearest_of_several_wins():
    best = best_market(
        "tonnel", {"tonnel": 10.0, "mrkt": 12.0, "portals": 14.0}, min_spread=0.05
    )
    assert best == "portals"


# --- Текст сообщения ------------------------------------------------------


def test_buy_message_carries_prices_of_every_market():
    """Решение о переносе принимает человек, и цены — единственное, на чём
    его можно принять."""
    notifier = Recorder(make_settings())
    notifier.buy(
        collection="Evil Eye",
        model="Pyromancy",
        number=42,
        market="tonnel",
        price_ton=8.0,
        fair_ton=11.0,
        roi=0.2,
        floors={"tonnel": 9.0, "mrkt": 12.0},
        paper=False,
    )

    text = notifier.sent[0]
    assert "Evil Eye #42" in text
    assert "tonnel: 9.00 TON" in text
    assert "mrkt: 12.00 TON" in text


def test_buy_message_explains_the_freeze_button():
    notifier = Recorder(make_settings())
    notifier.buy(
        collection="Evil Eye",
        model=None,
        number=None,
        market="tonnel",
        price_ton=8.0,
        fair_ton=None,
        roi=None,
        floors={"tonnel": 9.0, "mrkt": 12.0},
        paper=False,
    )

    text = notifier.sent[0]
    assert "Заморозить" in text
    assert "25 ⭐" in text, "цена переноса должна быть видна в решении"


def test_paper_purchases_are_marked():
    """Иначе бумажная сделка читается как настоящая."""
    notifier = Recorder(make_settings())
    notifier.buy(
        collection="Evil Eye",
        model=None,
        number=None,
        market="mrkt",
        price_ton=8.0,
        fair_ton=None,
        roi=None,
        floors={},
        paper=True,
    )
    assert "PAPER" in notifier.sent[0]


def test_no_advice_when_own_market_is_the_best():
    notifier = Recorder(make_settings())
    notifier.buy(
        collection="Evil Eye",
        model=None,
        number=None,
        market="mrkt",
        price_ton=8.0,
        fair_ton=None,
        roi=None,
        floors={"mrkt": 12.0, "tonnel": 9.0},
        paper=False,
    )
    assert "Заморозить" not in notifier.sent[0]


def test_spread_is_reported_once():
    """Разница держится часами. Сообщать о ней каждый цикл — это шум,
    который перестают читать, а вместе с ним перестают читать и важное.
    """
    notifier = Recorder(make_settings())
    for _ in range(5):
        notifier.spread(
            collection="Evil Eye", market="tonnel", floors={"tonnel": 9.0, "mrkt": 12.0}
        )
    assert len(notifier.sent) == 1


def test_disabled_notifications_send_nothing():
    settings = make_settings()
    settings.notify.enabled = False
    notifier = Recorder(settings)
    notifier.spread(
        collection="Evil Eye", market="tonnel", floors={"tonnel": 9.0, "mrkt": 12.0}
    )
    assert notifier.sent == []


def test_sell_notification_can_be_switched_off_alone():
    settings = make_settings()
    settings.notify.on_sell = False
    notifier = Recorder(settings)
    notifier.sold(
        collection="Evil Eye", market="mrkt", buy_ton=8.0, sell_ton=10.0,
        pnl_ton=1.6, paper=False,
    )
    assert notifier.sent == []


# --- Заморозка ------------------------------------------------------------


async def make_position(**overrides) -> int:
    base = {
        "market": "tonnel",
        "gift_external_id": "G-1",
        "collection": "Evil Eye",
        "buy_price_ton": 8.0,
        "status": "open",
        "paper": True,
    }
    base.update(overrides)
    async with session_scope() as session:
        position = Position(**base)
        session.add(position)
        await session.flush()
        return position.id


async def test_frozen_position_is_not_listed(db):
    """Главное свойство заморозки: подарок не продаётся, пока его переносят."""
    engine = TradingEngine(make_settings())
    position_id = await make_position()

    ok, _ = await engine.set_frozen(position_id, True)
    assert ok

    listed: list[int] = []

    async def fake_list(position):
        listed.append(position.id)
        return True

    engine._list_new = fake_list  # type: ignore[assignment]

    from app.trading.engine import CycleReport

    await engine._manage_positions(CycleReport())
    assert listed == [], "замороженную позицию выставлять нельзя"


async def test_unfrozen_position_goes_back_to_the_engine(db):
    engine = TradingEngine(make_settings())
    position_id = await make_position()

    await engine.set_frozen(position_id, True)
    await engine.set_frozen(position_id, False)

    listed: list[int] = []

    async def fake_list(position):
        listed.append(position.id)
        return True

    engine._list_new = fake_list  # type: ignore[assignment]

    from app.trading.engine import CycleReport

    await engine._manage_positions(CycleReport())
    assert listed == [position_id]


async def test_freezing_a_listed_position_takes_it_off_sale(db):
    """Перенести выставленный подарок нельзя — площадка его держит."""
    engine = TradingEngine(make_settings())
    position_id = await make_position(status="listed", listing_id="L-1", ask_price_ton=12.0)

    ok, _ = await engine.set_frozen(position_id, True)
    assert ok

    async with session_scope() as session:
        position = await session.get(Position, position_id)
        assert position.frozen is True
        assert position.status == "open"
        assert position.listing_id is None


async def test_live_freeze_removes_the_listing_from_the_market(db, monkeypatch):
    """В живом режиме мало пометить позицию — лот висит на площадке."""
    engine = TradingEngine(make_settings(paper=False))
    position_id = await make_position(
        status="listed", listing_id="L-1", ask_price_ton=12.0, paper=False
    )

    delisted: list[str] = []

    class FakeAdapter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def delist(self, listing_id: str) -> None:
            delisted.append(listing_id)

    from app.trading import engine as engine_mod

    monkeypatch.setattr(
        engine_mod.registry, "build_adapter", lambda market, settings: FakeAdapter()
    )

    ok, detail = await engine.set_frozen(position_id, True)

    assert ok
    assert delisted == ["L-1"]
    assert "снята с продажи" in detail


async def test_failed_delist_does_not_pretend_the_gift_is_free(db, monkeypatch):
    """Иначе человек пойдёт переносить подарок и упрётся в отказ площадки."""
    from app.adapters.base import MarketplaceError

    engine = TradingEngine(make_settings(paper=False))
    position_id = await make_position(
        status="listed", listing_id="L-1", ask_price_ton=12.0, paper=False
    )

    class BrokenAdapter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def delist(self, listing_id: str) -> None:
            raise MarketplaceError("площадка не отвечает")

    from app.trading import engine as engine_mod

    monkeypatch.setattr(
        engine_mod.registry, "build_adapter", lambda market, settings: BrokenAdapter()
    )

    ok, detail = await engine.set_frozen(position_id, True)

    assert not ok
    assert "снять с продажи не удалось" in detail

    async with session_scope() as session:
        position = await session.get(Position, position_id)
        assert position.frozen is False, "заморозка не состоялась — и вид делать нельзя"


# --- Кнопка в Telegram ----------------------------------------------------


def test_buy_message_carries_a_freeze_button():
    """Ради этой кнопки всё и затевалось: заморозить можно с телефона."""
    notifier = Recorder(make_settings())
    notifier.buy(
        collection="Evil Eye",
        model=None,
        number=None,
        market="tonnel",
        price_ton=8.0,
        fair_ton=None,
        roi=None,
        floors={},
        paper=False,
        position_id=7,
    )

    buttons = notifier.buttons[0]
    assert buttons is not None
    assert buttons[0][0]["callback_data"] == "freeze:7"


def test_callback_data_is_parsed():
    assert parse_callback("freeze:12") == ("freeze", 12)
    assert parse_callback("unfreeze:12") == ("unfreeze", 12)


def test_foreign_callback_data_is_rejected():
    """Данные приходят из сети — принимать их на веру нельзя."""
    for value in ("", "freeze:", "freeze:abc", "delete:12", "12", "freeze:12:extra"):
        assert parse_callback(value) is None


def test_button_label_says_what_will_happen():
    assert "Заморозить" in freeze_button(1, frozen=False)[0][0]["text"]
    assert freeze_button(1, frozen=True)[0][0]["text"] == "Разморозить"


async def test_button_press_freezes_the_position(db, monkeypatch):
    engine = TradingEngine(make_settings())
    engine.settings.notify.chat_id = 100
    position_id = await make_position()

    calls = _stub_bot_api(monkeypatch)
    await engine.bot._handle_callback(
        {
            "id": "CB1",
            "data": f"freeze:{position_id}",
            "message": {"message_id": 5, "chat": {"id": 100}},
        }
    )

    async with session_scope() as session:
        position = await session.get(Position, position_id)
        assert position.frozen is True

    assert calls["answerCallbackQuery"], "без ответа кнопка в Telegram зависает"
    assert calls["editMessageReplyMarkup"], "надпись должна смениться на «Разморозить»"


async def test_press_from_another_chat_changes_nothing(db, monkeypatch):
    """Иначе любой, кто найдёт бота, сможет распоряжаться позициями."""
    engine = TradingEngine(make_settings())
    engine.settings.notify.chat_id = 100
    position_id = await make_position()

    _stub_bot_api(monkeypatch)
    await engine.bot._handle_callback(
        {
            "id": "CB1",
            "data": f"freeze:{position_id}",
            "message": {"message_id": 5, "chat": {"id": 999}},
        }
    )

    async with session_scope() as session:
        position = await session.get(Position, position_id)
        assert position.frozen is False


async def test_start_from_a_foreign_chat_does_not_rebind(db, monkeypatch):
    engine = TradingEngine(make_settings())
    engine.settings.notify.chat_id = 100

    _stub_bot_api(monkeypatch)
    await engine.bot._handle_message({"chat": {"id": 999}, "text": "/start"})

    assert engine.settings.notify.chat_id == 100


def _stub_bot_api(monkeypatch) -> dict[str, list]:
    """Подменяем сеть: считаем вызовы вместо походов в Telegram."""
    from app import notify as notify_mod

    calls: dict[str, list] = {}

    async def fake_call(self, method, payload, *, timeout=20.0):
        calls.setdefault(method, []).append(payload)
        return {}

    monkeypatch.setattr(notify_mod.BotApi, "call", fake_call)
    monkeypatch.setattr(notify_mod, "bot_token", lambda: "123:TEST")
    return calls


async def test_closed_position_cannot_be_frozen(db):
    engine = TradingEngine(make_settings())
    position_id = await make_position(status="closed")

    ok, detail = await engine.set_frozen(position_id, True)
    assert not ok
    assert "закрыта" in detail


async def test_missing_position_is_reported(db):
    engine = TradingEngine(make_settings())
    ok, detail = await engine.set_frozen(999999, True)
    assert not ok
    assert "не найдена" in detail
