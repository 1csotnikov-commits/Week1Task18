"""Точка входа MCP-клиента: интерактивный режим и одноразовый режим ``--once``.

Тонкая обёртка над :mod:`client.logic`: вся работа с транспортом и
форматированием результатов находится в пакете ``client``, здесь — только
разбор аргументов командной строки, цикл ввода и вывод на экран.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import threading
from typing import Any

from client.logic import (
    MCPApp,
    ToolInfo,
    create_app,
    format_status,
    format_tools,
)

#: Единый источник описаний команд CLI. Добавление новой команды сюда
#: автоматически отражается в выводе ``/help``.
COMMANDS: dict[str, str] = {
    "/help": "показать список всех команд с описанием",
    "/tools": "повторно запросить у MCP-сервера список инструментов и вывести его",
    "/call": "вызвать инструмент MCP вручную (например: /call git_status или /call git_log limit=5)",
    "/weather": "показать погоду: /weather <city> [--days N] [--units metric|imperial]",
    "/ask-weather": "спросить агента о погоде: /ask-weather <city> [вопрос] (через LLM)",
    "/schedule": "управление задачами: /schedule list|reminder|weather|cancel|pause|resume ...",
    "/summary": "сводка по собранной погоде: /summary <city>|--all|--reminders [--days N]",
    "/schedules": "события планировщика: /schedules events [--since <ISO>]",
    "/status": "показать состояние MCP-соединения: подключение, имя сервера, версию протокола, инструменты, время последнего вызова",
    "/reconnect": "переустановить MCP-соединение: закрыть сессию, перезапустить сервер, initialize и заново получить инструменты",
    "/exit": "выйти из программы с корректным закрытием MCP-соединения (синоним: /quit)",
    "/quit": "выйти из программы с корректным закрытием MCP-соединения (синоним: /exit)",
}

#: Команды, завершающие работу.
EXIT_COMMANDS = {"/exit", "/quit"}


def print_help() -> None:
    """Выводит список всех команд CLI с описанием."""
    print("Доступные команды:")
    for command in COMMANDS:
        print(f"  {command:<12} — {COMMANDS[command]}")


def split_args(text: str) -> list[str]:
    """Разбивает строку на аргументы, поддерживая двойные кавычки.

    ``posix=False`` сохраняет обратные слэши (важно для Windows-путей).
    """
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        # Незакрытая кавычка — возвращаем грубое разбиение по пробелам.
        return text.split()


def _coerce_value(value: str, schema: dict[str, Any]) -> Any:
    """Приводит строковое значение аргумента к типу из JSON-схемы параметра."""
    type_ = schema.get("type")
    if isinstance(type_, list):
        non_null = [t for t in type_ if t != "null"]
        type_ = non_null[0] if non_null else "string"

    if type_ == "integer":
        return int(value)
    if type_ == "number":
        return float(value)
    if type_ == "boolean":
        return value.strip().lower() in ("true", "1", "yes", "да")
    if type_ in ("array", "object"):
        return json.loads(value)
    return value


def parse_call_args(tool: ToolInfo, tokens: list[str]) -> dict[str, Any]:
    """Преобразует токены вида ``key=value`` в словарь аргументов инструмента."""
    properties = (tool.input_schema or {}).get("properties", {})
    arguments: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"Аргумент должен быть в формате key=value, получено: {token!r}")
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        arguments[key] = _coerce_value(value, properties.get(key, {}))
    return arguments


async def handle_call(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/call <tool_name> [args...]``."""
    if not tokens:
        print("Укажите имя инструмента. Пример: /call git_status")
        return
    tool_name = tokens[0]
    tool = app.find_tool(tool_name)
    if tool is None:
        print(f"Инструмент '{tool_name}' не найден. Введите /tools для списка инструментов.")
        return
    try:
        arguments = parse_call_args(tool, tokens[1:])
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Ошибка разбора аргументов: {exc}")
        return

    try:
        result = await app.call_tool(tool_name, arguments)
    except Exception as exc:  # noqa: BLE001 - показываем понятную ошибку пользователю
        print(f"Ошибка при вызове инструмента '{tool_name}': {exc}")
        return

    print(f"=== Результат вызова {tool_name} ===")
    print(result)


def parse_weather_args(tokens: list[str]) -> tuple[str, int | None, str]:
    """Разбирает аргументы команды ``/weather <city> [--days N] [--units metric|imperial]``."""
    city_parts: list[str] = []
    days: int | None = None
    units = "metric"
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--days":
            if i + 1 >= len(tokens):
                raise ValueError("флаг --days требует значение (например, --days 5)")
            try:
                days = int(tokens[i + 1])
            except ValueError as exc:
                raise ValueError("значение --days должно быть целым числом") from exc
            i += 2
        elif token == "--units":
            if i + 1 >= len(tokens):
                raise ValueError("флаг --units требует значение (metric или imperial)")
            units = tokens[i + 1]
            i += 2
        elif token.startswith("--"):
            raise ValueError(f"неизвестный флаг: {token}")
        else:
            city_parts.append(token)
            i += 1

    city = " ".join(city_parts).strip()
    if not city:
        raise ValueError("укажите название города. Пример: /weather Moscow")
    return city, days, units


def parse_ask_weather_args(tokens: list[str]) -> tuple[str, str | None]:
    """Разбирает аргументы команды ``/ask-weather <city> [вопрос]``."""
    if not tokens:
        raise ValueError("укажите название города. Пример: /ask-weather Moscow")
    city = tokens[0]
    question = " ".join(tokens[1:]).strip() or None
    return city, question


async def handle_weather(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/weather <city> [--days N] [--units ...]``."""
    try:
        city, days, units = parse_weather_args(tokens)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return

    try:
        result = await app.get_weather(city, days, units)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка при вызове погодного инструмента: {exc}")
        return

    print(result)


async def handle_ask_weather(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/ask-weather <city> [вопрос]`` (через LLM)."""
    try:
        city, question = parse_ask_weather_args(tokens)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return

    print("Получение данных о погоде...")
    try:
        answer = await app.ask_weather(city, question)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")
        return

    print("=== Ответ агента ===")
    print(answer)


def _parse_int(value: str, name: str) -> int:
    """Разбирает целое число с понятным сообщением об ошибке."""
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} должно быть целым числом") from exc


async def _call_and_print(app: MCPApp, tool_name: str, arguments: dict[str, Any]) -> None:
    """Вызывает инструмент MCP и печатает результат."""
    try:
        result = await app.call_tool(tool_name, arguments)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")
        return
    print(result)


def parse_schedule_reminder_args(tokens: list[str]) -> dict[str, Any]:
    """Разбирает аргументы ``/schedule reminder "<текст>" [--at ISO | --in N] [--every N]``."""
    text_parts: list[str] = []
    at: str | None = None
    in_minutes: int | None = None
    every_seconds: int | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--at":
            if i + 1 >= len(tokens):
                raise ValueError("--at требует значение (ISO 8601)")
            at = tokens[i + 1]
            i += 2
        elif tok == "--in":
            if i + 1 >= len(tokens):
                raise ValueError("--in требует значение (минуты)")
            in_minutes = _parse_int(tokens[i + 1], "--in")
            i += 2
        elif tok == "--every":
            if i + 1 >= len(tokens):
                raise ValueError("--every требует значение (секунды)")
            every_seconds = _parse_int(tokens[i + 1], "--every")
            i += 2
        elif tok.startswith("--"):
            raise ValueError(f"неизвестный флаг: {tok}")
        else:
            text_parts.append(tok)
            i += 1

    text = " ".join(text_parts).strip()
    if not text:
        raise ValueError('укажите текст напоминания в кавычках. Пример: /schedule reminder "Проверить почту" --in 5')
    return {"text": text, "at": at, "in_minutes": in_minutes, "interval_seconds": every_seconds}


def parse_schedule_weather_args(tokens: list[str]) -> tuple[str, int]:
    """Разбирает аргументы ``/schedule weather <city> --every N`` (N — минуты)."""
    city_parts: list[str] = []
    every: int | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--every":
            if i + 1 >= len(tokens):
                raise ValueError("--every требует значение (минуты)")
            every = _parse_int(tokens[i + 1], "--every")
            i += 2
        elif tok.startswith("--"):
            raise ValueError(f"неизвестный флаг: {tok}")
        else:
            city_parts.append(tok)
            i += 1

    city = " ".join(city_parts).strip()
    if not city:
        raise ValueError("укажите город. Пример: /schedule weather Moscow --every 30")
    if every is None or every < 1:
        raise ValueError("укажите период --every в минутах (минимум 1)")
    return city, every


def parse_summary_args(tokens: list[str]) -> dict[str, Any]:
    """Разбирает аргументы ``/summary [<city>|--all|--reminders] [--days N]``."""
    city: str | None = None
    days = 1
    include_all_cities = False
    include_reminders = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--all":
            include_all_cities = True
            i += 1
        elif tok == "--reminders":
            include_reminders = True
            i += 1
        elif tok == "--days":
            if i + 1 >= len(tokens):
                raise ValueError("--days требует значение")
            days = _parse_int(tokens[i + 1], "--days")
            i += 2
        elif tok.startswith("--"):
            raise ValueError(f"неизвестный флаг: {tok}")
        else:
            city = tok
            i += 1
    return {
        "city": city,
        "days": days,
        "include_all_cities": include_all_cities,
        "include_reminders": include_reminders,
    }


async def handle_schedule(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/schedule <подкоманда> ...``."""
    if not tokens:
        print("Использование: /schedule list|reminder|weather|cancel|pause|resume ...")
        return
    sub = tokens[0].lower()
    rest = tokens[1:]
    if sub == "list":
        await _handle_schedule_list(app, rest)
    elif sub == "reminder":
        await _handle_schedule_reminder(app, rest)
    elif sub == "weather":
        await _handle_schedule_weather(app, rest)
    elif sub in ("cancel", "pause", "resume"):
        await _handle_schedule_status(app, sub, rest)
    else:
        print(f"Неизвестная подкоманда /schedule: {sub}. Введите /help.")


async def _handle_schedule_list(app: MCPApp, tokens: list[str]) -> None:
    status: str | None = None
    if tokens:
        if tokens[0] == "--status" and len(tokens) >= 2:
            status = tokens[1]
        elif tokens[0].startswith("--status="):
            status = tokens[0].split("=", 1)[1]
        else:
            print("Использование: /schedule list [--status active|paused|cancelled|completed]")
            return
    args: dict[str, Any] = {}
    if status:
        args["status"] = status
    await _call_and_print(app, "list_schedules", args)


async def _handle_schedule_reminder(app: MCPApp, tokens: list[str]) -> None:
    try:
        parsed = parse_schedule_reminder_args(tokens)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return
    args: dict[str, Any] = {"text": parsed["text"]}
    if parsed["at"] is not None:
        args["at"] = parsed["at"]
    if parsed["in_minutes"] is not None:
        args["in_minutes"] = parsed["in_minutes"]
    if parsed["interval_seconds"] is not None:
        args["interval_seconds"] = parsed["interval_seconds"]
    await _call_and_print(app, "schedule_reminder", args)


async def _handle_schedule_weather(app: MCPApp, tokens: list[str]) -> None:
    try:
        city, every = parse_schedule_weather_args(tokens)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return
    await _call_and_print(app, "schedule_weather_collection", {"city": city, "interval_minutes": every})


async def _handle_schedule_status(app: MCPApp, action: str, tokens: list[str]) -> None:
    if not tokens:
        print(f"Укажите id задачи. Пример: /schedule {action} <id>")
        return
    await _call_and_print(app, f"{action}_schedule", {"schedule_id": tokens[0]})


async def handle_summary(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/summary``."""
    try:
        parsed = parse_summary_args(tokens)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return
    await _call_and_print(
        app,
        "get_summary",
        {
            "city": parsed["city"],
            "days": parsed["days"],
            "include_reminders": parsed["include_reminders"],
            "include_all_cities": parsed["include_all_cities"],
        },
    )


async def handle_schedules_events(app: MCPApp, tokens: list[str]) -> None:
    """Обрабатывает команду ``/schedules events [--since <ISO>]``."""
    if not tokens or tokens[0].lower() != "events":
        print("Использование: /schedules events [--since <ISO>]")
        return
    since: str | None = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--since" and i + 1 < len(tokens):
            since = tokens[i + 1]
            i += 2
        else:
            print(f"Неизвестный аргумент: {tok}")
            return
    args: dict[str, Any] = {}
    if since:
        args["since"] = since
    await _call_and_print(app, "get_due_results", args)


async def run_once(app: MCPApp) -> None:
    """Одноразовый режим: подключиться, вывести инструменты и результат git_status, выйти."""
    await app.connect()
    print("Соединение установлено.")
    print()
    print(format_tools(app.tools))
    print()

    try:
        result = await app.call_tool("git_status")
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка при вызове инструмента 'git_status': {exc}")
    else:
        print("=== Результат вызова git_status ===")
        print(result)

    await app.disconnect()


async def run_interactive(app: MCPApp) -> None:
    """Интерактивный режим: соединение остаётся открытым, пока пользователь не выйдет.

    Ввод читается в отдельном (daemon) потоке и передаётся в очередь, чтобы
    event loop оставался свободным и мог выводить push-уведомления от сервера
    между вводами пользователя.
    """
    print_help()
    print()

    await app.connect()
    print("Соединение установлено.")
    print()
    print(format_tools(app.tools))
    print()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    stop_event = threading.Event()

    def reader() -> None:
        while not stop_event.is_set():
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            loop.call_soon_threadsafe(queue.put_nowait, line)

    threading.Thread(target=reader, daemon=True).start()

    try:
        while True:
            line = await queue.get()
            if line is None:
                break

            line = line.strip()
            if not line:
                continue

            tokens = split_args(line)
            command = tokens[0].lower()

            if command in EXIT_COMMANDS:
                break
            if command == "/help":
                print_help()
            elif command == "/tools":
                try:
                    tools = await app.refresh_tools()
                    print(format_tools(tools))
                except Exception as exc:  # noqa: BLE001
                    print(f"Ошибка при получении списка инструментов: {exc}")
            elif command == "/call":
                await handle_call(app, tokens[1:])
            elif command == "/weather":
                await handle_weather(app, tokens[1:])
            elif command == "/ask-weather":
                await handle_ask_weather(app, tokens[1:])
            elif command == "/schedule":
                await handle_schedule(app, tokens[1:])
            elif command == "/summary":
                await handle_summary(app, tokens[1:])
            elif command == "/schedules":
                await handle_schedules_events(app, tokens[1:])
            elif command == "/status":
                print(format_status(await app.status()))
            elif command == "/reconnect":
                try:
                    await app.reconnect()
                    print("Переподключение выполнено.")
                    print()
                    print(format_tools(app.tools))
                except Exception as exc:  # noqa: BLE001
                    print(f"Ошибка переподключения: {exc}")
            else:
                print(f"Неизвестная команда: {command}. Введите /help для списка команд.")
    finally:
        stop_event.set()

    await app.disconnect()
    print("MCP-соединение закрыто.")


def on_notification(message: str) -> None:
    """Печатает push-уведомление от сервера с префиксом [УВЕДОМЛЕНИЕ]."""
    print(f"[УВЕДОМЛЕНИЕ] {message}")


def _configure_stdio() -> None:
    """Переключает stdin/stdout/stderr на UTF-8, чтобы кириллица не ломалась."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Точка входа."""
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description="MCP-клиент (транспорт stdio)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Одноразовый режим: подключиться, вывести список инструментов и результат git_status, выйти.",
    )
    args = parser.parse_args()

    app = create_app(notification_callback=on_notification)
    try:
        if args.once:
            asyncio.run(run_once(app))
        else:
            asyncio.run(run_interactive(app))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")


if __name__ == "__main__":
    main()

