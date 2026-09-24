"""Провайдер LLM (DeepSeek) для команды ``/ask-weather``.

Использует OpenAI-совместимый chat completions API DeepSeek. API-ключ берётся
из переменной окружения ``DEEPSEEK_API_KEY``.
"""

from __future__ import annotations

import logging
import os

import httpx

# Не засорять вывод логами httpx (каждый запрос на уровне INFO).
logging.getLogger("httpx").setLevel(logging.WARNING)

#: Имя переменной окружения с API-ключом DeepSeek.
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
#: Эндпоинт chat completions DeepSeek (OpenAI-совместимый).
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
#: Модель по умолчанию.
DEFAULT_MODEL = "deepseek-chat"


class LLMError(Exception):
    """Ошибка при обращении к LLM (нет ключа, сеть, HTTP, некорректный ответ)."""


def get_api_key() -> str | None:
    """Возвращает API-ключ DeepSeek из окружения или None."""
    return os.environ.get(DEEPSEEK_API_KEY_ENV)


def ask(prompt: str, api_key: str | None = None, model: str = DEFAULT_MODEL) -> str:
    """Отправляет промт в DeepSeek и возвращает текст ответа.

    Бросает :class:`LLMError` с понятным сообщением при любой ошибке.
    """
    api_key = api_key or get_api_key()
    if not api_key:
        raise LLMError(
            f"Не задан API-ключ. Установите переменную окружения {DEEPSEEK_API_KEY_ENV}."
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        response = httpx.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=120.0)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.text[:300]
        except Exception:  # noqa: BLE001
            pass
        raise LLMError(f"LLM вернул ошибку HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.RequestError as exc:
        raise LLMError(f"Не удалось обратиться к LLM: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError("Некорректный ответ от LLM.") from exc
