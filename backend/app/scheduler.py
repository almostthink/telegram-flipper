"""Фоновое расписание.

Три задачи с разной частотой — чтобы не долбить площадки лишними
запросами и при этом не отставать от рынка:

* флоры — часто и дёшево, дают тренд и список коллекций;
* полный цикл — реже, тянет листинги и ленту сделок, считает сигналы
  и сопровождает позиции;
* уборка — раз в сутки, чтобы база не росла бесконечно.
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
        "Расписание запущено: флоры каждые %d мин, полный цикл каждые %d мин",
        FLOOR_SCAN_MINUTES,
        FULL_CYCLE_MINUTES,
    )
    return scheduler


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
