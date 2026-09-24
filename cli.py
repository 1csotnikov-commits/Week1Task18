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
        print(f"  {command:<10} — {COMMANDS[command]}")


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
    """Интерактивный режим: соединение остаётся открытым, пока пользователь не выйдет."""
    print_help()
    print()

    await app.connect()
    print("Соединение установлено.")
    print()
    print(format_tools(app.tools))
    print()

    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
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

    await app.disconnect()
    print("MCP-соединение закрыто.")


def main() -> None:
    """Точка входа."""
    parser = argparse.ArgumentParser(
        description="MCP-клиент для Git-сервера (транспорт stdio)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Одноразовый режим: подключиться, вывести список инструментов и результат git_status, выйти.",
    )
    args = parser.parse_args()

    app = create_app()
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

