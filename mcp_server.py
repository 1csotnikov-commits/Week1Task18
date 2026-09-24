"""MCP-сервер, предоставляющий инструменты для работы с локальным Git-репозиторием.

Транспорт — stdio. Сервер запускается как подпроцесс MCP-клиентом
(`python mcp_server.py`) и обменивается с ним JSON-RPC сообщениями через
stdin/stdout. Поэтому здесь запрещено писать что-либо в stdout (выводом
пользуется транспорт); логи идут в stderr.

Регистрация нового инструмента сводится к добавлению функции с декоратором
``@server.tool()`` — это одна из точек расширения проекта.
"""

from __future__ import annotations

import httpx
import logging
import os
import shutil
import subprocess
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

# httpx логирует каждый HTTP-запрос на уровне INFO и засоряет stderr сервера;
# оставляем только предупреждения и ошибки.
logging.getLogger("httpx").setLevel(logging.WARNING)

#: Имя переменной окружения, задающей путь к Git-репозиторию по умолчанию.
GIT_REPO_PATH_ENV = "GIT_REPO_PATH"

#: Формат вывода ``git log``. Спецсимволы ``%x00``/``%x1e`` — это *литеральные*
#: последовательности в формате git (сам git подставит байты NUL и RS в вывод),
#: поэтому аргумент командной строки остаётся чистым ASCII и работает на Windows.
#: Состав: hash, автор (имя), автор (email), дата, тема коммита.
_LOG_PRETTY = "format:%H%x00%an%x00%ae%x00%ad%x00%s%x1e"

#: Разделитель записей в выводе ``git log`` (символ RS, не встречается в данных).
_LOG_RECORD_SEP = "\x1e"
#: Разделитель полей внутри одной записи ``git log`` (символ NUL).
_LOG_FIELD_SEP = "\x00"


class GitError(Exception):
    """Ошибка при работе с Git: не установлен, не репозиторий или сбой команды."""


def _resolve_repo_path(repo_path: str | None) -> str:
    """Определяет путь к репозиторию: явный аргумент, затем ``GIT_REPO_PATH``, затем cwd."""
    if repo_path:
        return repo_path
    return os.environ.get(GIT_REPO_PATH_ENV) or os.getcwd()


def _ensure_git_available() -> None:
    """Проверяет, что ``git`` доступен в PATH, иначе бросает :class:`GitError`."""
    if shutil.which("git") is None:
        raise GitError(
            "Git не найден в PATH. Установите git и добавьте его в переменную окружения PATH."
        )


def _run_git(repo_path: str, args: list[str]) -> str:
    """Запускает ``git`` для указанного репозитория и возвращает stdout.

    Бросает :class:`GitError` с понятным сообщением, если git недоступен,
    путь не существует, путь не является репозиторием или команда завершилась
    с ошибкой.
    """
    _ensure_git_available()

    if not os.path.exists(repo_path):
        raise GitError(f"Путь не существует: {repo_path}")
    if not os.path.isdir(repo_path):
        raise GitError(f"Путь не является директорией: {repo_path}")

    command = ["git", "-C", repo_path, *args]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        if "not a git repository" in message:
            raise GitError(f"Указанный путь не является Git-репозиторием: {repo_path}")
        raise GitError(f"Команда 'git {' '.join(args)}' завершилась с ошибкой: {message or 'неизвестная ошибка'}")

    return proc.stdout


def _parse_git_log(raw: str) -> list[dict[str, str]]:
    """Разбирает вывод ``git log`` с разделителями в список словарей коммитов."""
    commits: list[dict[str, str]] = []
    for record in raw.split(_LOG_RECORD_SEP):
        record = record.strip()
        if not record:
            continue
        fields = record.split(_LOG_FIELD_SEP)
        if len(fields) != 5:
            continue
        commit_hash, name, email, date, subject = fields
        commits.append(
            {
                "hash": commit_hash,
                "author": f"{name} <{email}>".strip(),
                "date": date,
                "message": subject,
            }
        )
    return commits


# --- Погода (Open-Meteo, без API-ключа) ---

#: Базовые эндпоинты Open-Meteo (ключ и регистрация не требуются).
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: Допустимые системы единиц и их отображение в параметрах/выводе.
_UNITS_MAP = {
    "metric": {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "temp": "°C", "wind": "км/ч"},
    "imperial": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "temp": "°F", "wind": "mph"},
}

#: Упрощённые русские описания кодов погоды WMO.
_WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь/туман",
    51: "лёгкая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


class WeatherError(Exception):
    """Ошибка доступа к API погоды (сеть, HTTP, некорректный ответ)."""


class CityNotFoundError(Exception):
    """Город не найден в геокодинге."""


def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Выполняет GET-запрос и возвращает JSON. Бросает :class:`WeatherError` при ошибке."""
    try:
        response = httpx.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise WeatherError(f"HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise WeatherError(str(exc)) from exc
    except ValueError as exc:
        raise WeatherError("некорректный ответ API (не JSON)") from exc


def _geocode(city: str) -> tuple[float, float, str]:
    """Возвращает (широта, долгота, отображаемое имя) для города.

    Бросает :class:`CityNotFoundError`, если город не найден.
    """
    data = _fetch_json(
        GEOCODING_URL,
        {"name": city, "count": 1, "language": "ru", "format": "json"},
    )
    results = data.get("results") or []
    if not results:
        raise CityNotFoundError(f'Город "{city}" не найден.')
    first = results[0]
    return first["latitude"], first["longitude"], first.get("name") or city


def _weather_code_description(code: int | None) -> str:
    """Возвращает русское описание кода погоды WMO."""
    if code is None:
        return "неизвестно"
    return _WEATHER_CODES.get(int(code), f"код {code}")


def _current_weather_params(units: str) -> dict[str, Any]:
    """Параметры запроса текущей погоды для выбранной системы единиц."""
    unit = _UNITS_MAP[units]
    return {
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "timezone": "auto",
        "temperature_unit": unit["temperature_unit"],
        "wind_speed_unit": unit["wind_speed_unit"],
    }


def _format_current_weather(city_display: str, current: dict[str, Any], units: str) -> str:
    """Форматирует текущую погоду в читаемый текст."""
    unit = _UNITS_MAP[units]
    lines = [f"Текущая погода в городе {city_display}:"]
    lines.append(f"  Температура: {current.get('temperature_2m')}{unit['temp']}")
    lines.append(f"  Ощущается как: {current.get('apparent_temperature')}{unit['temp']}")
    lines.append(f"  Ветер: {current.get('wind_speed_10m')} {unit['wind']}")
    lines.append(f"  Влажность: {current.get('relative_humidity_2m')}%")
    lines.append(f"  Осадки: {current.get('precipitation')} мм")
    lines.append(f"  Описание: {_weather_code_description(current.get('weather_code'))}")
    return "\n".join(lines)


def _format_forecast(city_display: str, daily: dict[str, Any], units: str) -> str:
    """Форматирует прогноз погоды по дням в читаемый текст."""
    unit = _UNITS_MAP[units]
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_sum", [])
    wind = daily.get("wind_speed_10m_max", [])
    codes = daily.get("weather_code", [])

    lines = [f"Прогноз погоды в городе {city_display} на {len(times)} дн.:"]
    for i, date in enumerate(times):
        code = codes[i] if i < len(codes) else None
        lines.append(
            f"  {date}: {tmin[i]}{unit['temp']} … {tmax[i]}{unit['temp']}, "
            f"осадки {precip[i]} мм, ветер до {wind[i]} {unit['wind']}, "
            f"{_weather_code_description(code)}"
        )
    return "\n".join(lines)


server = MCPServer(
    name="git-mcp-server",
    title="Git MCP Server",
    description="MCP-сервер, предоставляющий инструменты для работы с локальным Git-репозиторием.",
    version="0.1.0",
)


@server.tool(structured_output=False)
def git_status() -> str:
    """Возвращает статус Git-репозитория: вывод ``git status --short`` и ``git status -sb``.

    Репозиторий берётся из переменной окружения ``GIT_REPO_PATH`` или, если она
    не задана, из текущей директории.
    """
    try:
        repo_path = _resolve_repo_path(None)
        short = _run_git(repo_path, ["status", "--short"]).rstrip("\n")
        branch = _run_git(repo_path, ["status", "-sb"]).rstrip("\n")
        return (
            f"Репозиторий: {repo_path}\n\n"
            f"=== git status --short ===\n{short or '(нет изменений)'}\n\n"
            f"=== git status -sb ===\n{branch or '(нет изменений)'}"
        )
    except GitError as exc:
        return f"Ошибка: {exc}"
    except Exception as exc:  # noqa: BLE001 - не даём серверу упасть
        return f"Непредвиденная ошибка: {exc}"


@server.tool()
def git_log(
    limit: Annotated[
        int,
        Field(description="Сколько последних коммитов вернуть", ge=1),
    ] = 10,
    repo_path: Annotated[
        str | None,
        Field(description="Путь к репозиторию; если не задан, используется GIT_REPO_PATH или текущая директория"),
    ] = None,
) -> list[dict[str, str]]:
    """Возвращает список последних N коммитов репозитория: hash, author, date, message.

    Возвращаемое значение — список словарей, каждый с ключами ``hash``, ``author``,
    ``date`` и ``message``.
    """
    try:
        if limit < 1:
            return [{"error": "Параметр limit должен быть положительным числом."}]

        path = _resolve_repo_path(repo_path)
        raw = _run_git(
            path,
            [
                "log",
                f"-n{limit}",
                "--date=iso-strict",
                f"--pretty={_LOG_PRETTY}",
            ],
        )
        commits = _parse_git_log(raw)
        if not commits:
            return [{"message": "В репозитории нет коммитов."}]
        return commits
    except GitError as exc:
        return [{"error": str(exc)}]
    except Exception as exc:  # noqa: BLE001 - не даём серверу упасть
        return [{"error": f"Непредвиденная ошибка: {exc}"}]


@server.tool(structured_output=False)
def get_current_weather(
    city: Annotated[str, Field(description="Название города (например, Moscow, London)")],
    units: Annotated[
        str,
        Field(description="Единицы измерения: metric (Цельсий, км/ч) или imperial (Фаренгейт, mph)"),
    ] = "metric",
) -> str:
    """Возвращает текстовое описание текущей погоды для указанного города.

    Данные берутся из Open-Meteo (без API-ключа): температура, ветер, влажность,
    осадки и краткое описание.
    """
    try:
        if not city or not city.strip():
            return "Некорректный параметр: город не может быть пустым."
        if units not in _UNITS_MAP:
            return "Некорректный параметр: units должен быть 'metric' или 'imperial'."

        city = city.strip()
        lat, lon, city_display = _geocode(city)
        data = _fetch_json(
            FORECAST_URL,
            {"latitude": lat, "longitude": lon, **_current_weather_params(units)},
        )
        current = data.get("current") or {}
        return _format_current_weather(city_display, current, units)
    except CityNotFoundError as exc:
        return str(exc)
    except WeatherError as exc:
        return f"Не удалось получить данные о погоде: {exc}"
    except Exception as exc:  # noqa: BLE001 - не даём серверу упасть
        return f"Непредвиденная ошибка: {exc}"


@server.tool(structured_output=False)
def get_forecast(
    city: Annotated[str, Field(description="Название города (например, Moscow, London)")],
    days: Annotated[int, Field(description="Количество дней прогноза (максимум 7)")] = 3,
    units: Annotated[str, Field(description="Единицы измерения: metric или imperial")] = "metric",
) -> str:
    """Возвращает текстовый прогноз погоды на N дней для указанного города.

    По каждому дню: дата, температура (min/max), осадки, ветер и краткое описание.
    """
    try:
        if not city or not city.strip():
            return "Некорректный параметр: город не может быть пустым."
        if days < 1:
            return "Некорректный параметр: days должен быть не меньше 1."
        if days > 7:
            return "Некорректный параметр: days не может быть больше 7."
        if units not in _UNITS_MAP:
            return "Некорректный параметр: units должен быть 'metric' или 'imperial'."

        city = city.strip()
        lat, lon, city_display = _geocode(city)
        unit = _UNITS_MAP[units]
        data = _fetch_json(
            FORECAST_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
                "forecast_days": days,
                "timezone": "auto",
                "temperature_unit": unit["temperature_unit"],
                "wind_speed_unit": unit["wind_speed_unit"],
            },
        )
        daily = data.get("daily") or {}
        return _format_forecast(city_display, daily, units)
    except CityNotFoundError as exc:
        return str(exc)
    except WeatherError as exc:
        return f"Не удалось получить данные о погоде: {exc}"
    except Exception as exc:  # noqa: BLE001 - не даём серверу упасть
        return f"Непредвиденная ошибка: {exc}"


if __name__ == "__main__":
    # stdio — единственный используемый транспорт.
    server.run(transport="stdio")
