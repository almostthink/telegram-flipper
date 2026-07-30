"""Фоновое расписание.

Четыре задачи с разной частотой — чтобы не долбить площадки лишними
запросами и при этом не отставать от рынка:

* быстрая петля — раз в несколько секунд, один запрос к ленте свежих
  лотов; реакция на недооценённое предложение, пока оно ещё стоит;
* флоры — часто и дёшево, дают тренд и список коллекций;
* полный цикл — реже, тянет листинги и ленту сделок, считает сигналы
  и сопровождает позиции;
* уборка — раз в сутки, чтобы база не росла бесконечно.

Быстрая петля намеренно ничего не собирает сама: она пользуется тем,
что уже накопили медленные, и потому успевает за секунды.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.api.deps import get_engine
from app.storage.db import vacuum_old_data

log = logging.getLogger(__name__)

FLOOR_SCAN_MINUTES = 5
FULL_CYCLE_MINUTES = 20
CLEANUP_HOURS = 24

_scheduler: AsyncIOScheduler | None = None


async def _watch() -> None:
    engine = get_engine()
    if not engine.settings.watch_enabled:
        return
    report = await engine.run_watch()
    if report.bought or report.errors:
        log.info(
            "Быстрая петля: новых %d, прошло отбор %d, куплено %d",
            report.seen, report.passed, report.bought,
        )


async def _scan_floors() -> None:
    engine = get_engine()
    stats = await engine.scanner.scan_floors()
    if stats.errors:
        log.debug("Сбор флоров с замечаниями: %s", stats.errors[:3])


async def _full_cycle() -> None:
    await get_engine().run_cycle(deep=True)


async def _cleanup() -> None:
    await vacuum_old_data(keep_days=60)


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _watch,
        IntervalTrigger(seconds=get_engine().settings.watch_interval_sec),
        id="watch",
        # Тик длиннее периода — не повод копить очередь: рынок уже ушёл,
        # и следующий тик всё равно увидит актуальную выдачу.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10,
    )
    scheduler.add_job(
        _scan_floors,
        IntervalTrigger(minutes=FLOOR_SCAN_MINUTES),
        id="floors",
        max_instances=1,
        # Пропущенный запуск не копим: рынок ушёл вперёд, старый скан бесполезен.
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _full_cycle,
        IntervalTrigger(minutes=FULL_CYCLE_MINUTES),
        id="cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        _cleanup,
        IntervalTrigger(hours=CLEANUP_HOURS),
        id="cleanup",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "Расписание запущено: быстрая петля раз в %.0fс, флоры каждые %d мин, "
        "полный цикл каждые %d мин",
        get_engine().settings.watch_interval_sec,
        FLOOR_SCAN_MINUTES,
        FULL_CYCLE_MINUTES,
    )
    return scheduler


def reschedule_watch(seconds: float) -> None:
    """Меняем период быстрой петли на лету.

    Без этого настройка в интерфейсе сохранялась бы в конфиг и молча не
    действовала до перезапуска — худший вид неработающей ручки.
    """
    if _scheduler is None:
        return
    job = _scheduler.get_job("watch")
    if job is None:
        return
    job.reschedule(trigger=IntervalTrigger(seconds=seconds))
    log.info("Быстрая петля: период изменён на %.0fс", seconds)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Расписание остановлено")


def job_status() -> list[dict]:
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in _scheduler.get_jobs()
    ]
