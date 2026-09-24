"""Агрегация результатов и формирование summary через LLM."""

from __future__ import annotations

import json
from typing import Any

from scheduler.db import Database, days_ago_iso


def compute_aggregates(
    db: Database,
    city: str | None = None,
    days: int = 1,
    include_reminders: bool = False,
    include_all_cities: bool = False,
) -> dict[str, Any]:
    """Считает агрегаты через SQL-запросы к ``results``."""
    since = days_ago_iso(days)
    raw: dict[str, Any] = {}
    if include_all_cities or city:
        raw["weather"] = db.weather_aggregates(since, city=None if include_all_cities else city)
    if include_reminders:
        raw["reminders"] = {"count": db.reminder_count(since)}
    return raw


def build_summary(raw: dict[str, Any]) -> str:
    """Формирует человекочитаемое summary через LLM (DeepSeek).

    Если ключ не задан, возвращает пометку о недоступности LLM.
    """
    from llm.provider import LLMError, ask

    data = json.dumps(raw, ensure_ascii=False, default=str)
    prompt = (
        f"Вот агрегированные данные о погоде: {data}. "
        "Сформулируй краткое человекочитаемое summary на русском."
    )
    try:
        return ask(prompt)
    except LLMError as exc:
        return f"(LLM недоступен: {exc})"


def get_summary(
    db: Database,
    city: str | None = None,
    days: int = 1,
    include_reminders: bool = False,
    include_all_cities: bool = False,
) -> dict[str, Any]:
    """Возвращает структуру ``{"raw": агрегаты, "summary": текст}``."""
    raw = compute_aggregates(db, city, days, include_reminders, include_all_cities)
    summary = build_summary(raw)
    return {"raw": raw, "summary": summary}
