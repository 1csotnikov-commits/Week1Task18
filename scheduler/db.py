"""SQLite-хранилище планировщика: схема, миграции и CRUD.

Используется синхронный ``sqlite3`` из стандартной библиотеки: операции быстрые,
а вызовы из асинхронного кода оборачиваются в ``asyncio.to_thread``. Отдельная
библиотека ``aiosqlite`` не требуется.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Путь к БД по умолчанию — ``scheduler.db`` рядом с ``mcp_server.py``.
DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "scheduler.db")

#: Имя переменной окружения, переопределяющей путь к БД.
DB_PATH_ENV = "SCHEDULER_DB_PATH"


SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    params TEXT NOT NULL,
    interval_seconds INTEGER,
    run_at TEXT,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    schedule_id TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(next_run_at, status);
CREATE INDEX IF NOT EXISTS idx_results_schedule ON results(schedule_id);
CREATE INDEX IF NOT EXISTS idx_results_run_at ON results(run_at);
CREATE INDEX IF NOT EXISTS idx_events_ack ON events(acknowledged);
"""


def resolve_db_path() -> str:
    """Возвращает путь к файлу БД: ``SCHEDULER_DB_PATH`` или путь по умолчанию."""
    return os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH


def now_utc() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Текущее время в UTC в виде ISO-строки."""
    return now_utc().isoformat()


def parse_dt(value: str) -> datetime:
    """Разбирает ISO-строку в aware-datetime (naive считается UTC)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc_iso(value: str) -> str:
    """Нормализует ISO-строку к UTC-ISO."""
    return parse_dt(value).isoformat()


def days_ago_iso(days: int) -> str:
    """ISO-строка для момента ``days`` дней назад."""
    return (now_utc() - timedelta(days=days)).isoformat()


class Database:
    """CRUD-обёртка над SQLite для планировщика."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or resolve_db_path()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Any:
        """Контекстный менеджер соединения (коммит/закрытие)."""
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # --- schedules ---

    def create_schedule(
        self,
        type_: str,
        params: dict[str, Any],
        next_run_at: str,
        interval_seconds: int | None = None,
        run_at: str | None = None,
    ) -> str:
        """Создаёт задачу и возвращает её id."""
        schedule_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO schedules "
                "(id, type, params, interval_seconds, run_at, next_run_at, last_run_at, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 'active', ?)",
                (
                    schedule_id,
                    type_,
                    json.dumps(params, ensure_ascii=False),
                    interval_seconds,
                    run_at,
                    next_run_at,
                    now_iso(),
                ),
            )
        return schedule_id

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return self._schedule_to_dict(row) if row else None

    def list_schedules(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedules"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._schedule_to_dict(r) for r in rows]

    def list_due_schedules(self, now: str) -> list[dict[str, Any]]:
        """Активные задачи, у которых наступило время запуска (``next_run_at <= now``)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE status = 'active' AND next_run_at <= ? ORDER BY next_run_at",
                (now,),
            ).fetchall()
        return [self._schedule_to_dict(r) for r in rows]

    def set_status(self, schedule_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE schedules SET status = ? WHERE id = ?", (status, schedule_id))

    def set_next_run(self, schedule_id: str, next_run_at: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run_at, schedule_id))

    def set_last_run(self, schedule_id: str, last_run_at: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (last_run_at, schedule_id))

    # --- results ---

    def add_result(self, schedule_id: str, run_at: str, status: str, payload: Any) -> str:
        result_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO results (id, schedule_id, run_at, status, payload) VALUES (?, ?, ?, ?, ?)",
                (result_id, schedule_id, run_at, status, json.dumps(payload, ensure_ascii=False, default=str)),
            )
        return result_id

    def list_results(self, since: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT r.*, s.type AS schedule_type FROM results r LEFT JOIN schedules s ON r.schedule_id = s.id"
        args: list[Any] = []
        if since:
            sql += " WHERE r.run_at >= ?"
            args.append(since)
        sql += " ORDER BY r.run_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._result_to_dict(r) for r in rows]

    # --- events ---

    def add_event(self, type_: str, message: str, schedule_id: str | None = None) -> str:
        event_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (id, ts, type, message, schedule_id, acknowledged) VALUES (?, ?, ?, ?, ?, 0)",
                (event_id, now_iso(), type_, message, schedule_id),
            )
        return event_id

    def list_events(self, acknowledged: int | None = 0, since: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if acknowledged is not None:
            sql += " AND acknowledged = ?"
            args.append(acknowledged)
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_events(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._conn() as conn:
            conn.executemany("UPDATE events SET acknowledged = 1 WHERE id = ?", [(e,) for e in event_ids])

    # --- aggregates ---

    def weather_aggregates(self, since: str, city: str | None = None) -> dict[str, Any]:
        """Агрегаты по собранным данным о погоде (среднее/min/max, осадки, срабатывания)."""
        sql = (
            "SELECT COUNT(*) AS count, "
            "AVG(CAST(json_extract(r.payload, '$.temperature_2m') AS REAL)) AS avg_temp, "
            "MIN(CAST(json_extract(r.payload, '$.temperature_2m') AS REAL)) AS min_temp, "
            "MAX(CAST(json_extract(r.payload, '$.temperature_2m') AS REAL)) AS max_temp, "
            "SUM(CAST(json_extract(r.payload, '$.precipitation') AS REAL)) AS total_precipitation "
            "FROM results r JOIN schedules s ON r.schedule_id = s.id "
            "WHERE s.type = 'weather_collection' AND r.status = 'success' AND r.run_at >= ?"
        )
        args: list[Any] = [since]
        if city:
            sql += " AND json_extract(s.params, '$.city') = ?"
            args.append(city)
        with self._conn() as conn:
            row = conn.execute(sql, args).fetchone()
        return {
            "count": row["count"] or 0,
            "avg_temp": round(row["avg_temp"], 2) if row["avg_temp"] is not None else None,
            "min_temp": row["min_temp"],
            "max_temp": row["max_temp"],
            "total_precipitation": round(row["total_precipitation"], 2) if row["total_precipitation"] is not None else 0.0,
        }

    def reminder_count(self, since: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM results r JOIN schedules s ON r.schedule_id = s.id "
                "WHERE s.type = 'reminder' AND r.status = 'success' AND r.run_at >= ?",
                (since,),
            ).fetchone()
        return row["c"] or 0

    # --- row helpers ---

    @staticmethod
    def _schedule_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["params"] = json.loads(data["params"] or "{}")
        return data

    @staticmethod
    def _result_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data["payload"] or "{}")
        return data


