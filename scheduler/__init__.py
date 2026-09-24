"""Пакет планировщика (псевдо-24/7) для MCP-сервера.

Содержит хранилище (``db``), фоновый планировщик (``scheduler``), логику задач
(``jobs``) и агрегацию/LLM-summary (``summary``).
"""

from __future__ import annotations

import logging
from typing import Any

import mcp_types as types

from scheduler.db import Database

logger = logging.getLogger("scheduler")


class AppContext:
    """Разделяемое состояние приложения: БД + сессия для push-уведомлений.

    Сессия захватывается middleware'ом при первом входящем запросе и
    используется фоновым планировщиком для отправки ``notifications/message``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.session: Any = None  # ServerSession | None

    def set_session(self, session: Any) -> None:
        self.session = session

    async def emit(self, event_type: str, message: str, schedule_id: str | None = None) -> None:
        """Сохраняет событие в БД и отправляет push-уведомление клиенту (если сессия есть)."""
        self.db.add_event(event_type, message, schedule_id)
        if self.session is None:
            return
        try:
            await self.session.send_notification(
                types.LoggingMessageNotification(
                    params=types.LoggingMessageNotificationParams(
                        level="info",
                        data=f"{event_type}: {message}",
                        logger="scheduler",
                    )
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("не удалось отправить уведомление клиенту")


__all__ = ["Database", "AppContext"]
