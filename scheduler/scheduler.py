"""Фоновый планировщик: asyncio-задача, обрабатывающая due-задачи.

Один экземпляр запускается при старте MCP-сервера (через lifespan) и каждые
``TICK_SECONDS`` секунд проверяет таблицу ``schedules``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from scheduler.db import Database, now_iso, now_utc, parse_dt
from scheduler.jobs import execute_job

logger = logging.getLogger("scheduler")

TICK_SECONDS = 20


class Scheduler:
    """Периодически выполняет задачи, у которых наступило время запуска."""

    def __init__(self, db: Database, app_context: Any) -> None:
        self.db = db
        self.app_context = app_context
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Запускает фоновую задачу планировщика."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Останавливает фоновую задачу планировщика."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        await self._catch_up_missed()
        while True:
            try:
                await self._process_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("ошибка при обработке due-задач")
            await asyncio.sleep(TICK_SECONDS)

    async def _catch_up_missed(self) -> None:
        """При старте помечает пропущенные срабатывания как 'missed' (без выполнения).

        Сама задача остаётся due и будет выполнена один раз следующим тиком.
        """
        now_dt = now_utc()
        now = now_dt.isoformat()
        due = await asyncio.to_thread(self.db.list_due_schedules, now)
        for s in due:
            interval = s.get("interval_seconds")
            if not interval:
                continue
            try:
                next_run = parse_dt(s["next_run_at"])
            except Exception:  # noqa: BLE001
                continue
            elapsed = (now_dt - next_run).total_seconds()
            missed = int(elapsed // interval)
            for _ in range(missed):
                await asyncio.to_thread(
                    self.db.add_result,
                    s["id"],
                    now,
                    "missed",
                    {"reason": "пропущено (сервер был выключен)"},
                )

    async def _process_due(self) -> None:
        now = now_iso()
        due = await asyncio.to_thread(self.db.list_due_schedules, now)
        for s in due:
            try:
                await self._run_schedule(s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("задача %s упала", s.get("id"))

    async def _run_schedule(self, s: dict[str, Any]) -> None:
        schedule_id = s["id"]
        type_ = s["type"]
        now = now_iso()

        try:
            payload = await asyncio.to_thread(execute_job, s)
            status = "success"
            event_type = "reminder_fired" if type_ == "reminder" else "collection_done"
            description = _success_description(type_, payload)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            payload = {"error": str(exc)}
            event_type = "error"
            description = f"ошибка: {exc}"

        await asyncio.to_thread(self.db.add_result, schedule_id, now, status, payload)
        await asyncio.to_thread(self.db.set_last_run, schedule_id, now)

        interval = s.get("interval_seconds")
        if type_ == "reminder" and not interval:
            # Разовое напоминание → терминальное состояние.
            await asyncio.to_thread(self.db.set_status, schedule_id, "completed")
        else:
            # Периодическая задача: следующий запуск через интервал.
            next_run = (now_utc() + timedelta(seconds=interval)).isoformat()
            await asyncio.to_thread(self.db.set_next_run, schedule_id, next_run)

        await self.app_context.emit(event_type, description, schedule_id)


def _success_description(type_: str, payload: dict[str, Any]) -> str:
    if type_ == "reminder":
        return payload.get("text", "")
    return f"собрана погода для {payload.get('city', '?')}"
