"""Низкоуровневый транспортный слой MCP-клиента.

Отвечает за запуск MCP-сервера как подпроцесса, установку соединения по stdio,
выполнение ``initialize``, получение списка инструментов и вызов инструментов.

Здесь нет никакой бизнес-логики (что именно вызывать и как форматировать
результат) — она вынесена в :mod:`client.logic`. Это позволяет в будущем
подменить/расширить транспорт, не трогая высокоуровневую логику.

Точки расширения:
- класс :class:`MCPSession` принимает готовые параметры подпроцесса
  (``command``/``args``/``cwd``/``env``), поэтому легко переключиться на другой
  сервер или, при необходимости, на другой транспорт;
- методы ``connect``, ``list_tools``, ``call_tool`` изолированы и могут быть
  обёрнуты (например, для retry/отложенного выполнения) без изменения вызовов.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mcp_types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: Команда, которой клиент запускает сервер (по требованию задания — "python").
DEFAULT_COMMAND = "python"
#: Аргументы запуска сервера.
DEFAULT_ARGS = ["mcp_server.py"]
#: Каталог проекта, в котором лежит ``mcp_server.py``.
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


class MCPSession:
    """Обёртка над MCP-сессией: управляет жизненным циклом соединения.

    Клиент сам запускает сервер как подпроцесс через :func:`mcp.client.stdio.stdio_client`
    и общается с ним через :class:`mcp.ClientSession`.
    """

    def __init__(
        self,
        command: str = DEFAULT_COMMAND,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._params = StdioServerParameters(
            command=command,
            args=args if args is not None else list(DEFAULT_ARGS),
            cwd=cwd if cwd is not None else PROJECT_ROOT,
            env=env,
        )
        self._session: ClientSession | None = None
        self._stdio_ctx = None
        self._session_ctx = None
        self._initialize_result: types.InitializeResult | None = None

    @property
    def is_connected(self) -> bool:
        """Подключён ли клиент к серверу в данный момент."""
        return self._session is not None

    @property
    def server_info(self) -> types.Implementation | None:
        """Информация о сервере, полученная при ``initialize`` (или None)."""
        if self._initialize_result is None:
            return None
        return self._initialize_result.server_info

    @property
    def protocol_version(self) -> str | None:
        """Версия протокола MCP, согласованная при ``initialize`` (или None)."""
        if self._initialize_result is None:
            return None
        return self._initialize_result.protocol_version

    async def connect(self) -> None:
        """Запускает сервер-подпроцесс, устанавливает соединение и выполняет ``initialize``."""
        if self.is_connected:
            await self.close()

        self._stdio_ctx = stdio_client(self._params)
        try:
            read_stream, write_stream = await self._stdio_ctx.__aenter__()
        except BaseException:
            self._stdio_ctx = None
            raise

        try:
            self._session_ctx = ClientSession(read_stream, write_stream)
            self._session = await self._session_ctx.__aenter__()
            self._initialize_result = await self._session.initialize()
        except BaseException:
            await self._cleanup()
            raise

    async def list_tools(self) -> list[types.Tool]:
        """Возвращает список инструментов, объявленных сервером."""
        self._ensure_connected()
        result = await self._session.list_tools()
        return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        """Вызывает инструмент сервера и возвращает сырой результат вызова."""
        self._ensure_connected()
        return await self._session.call_tool(name, arguments or {})

    async def close(self) -> None:
        """Корректно закрывает сессию и завершает подпроцесс сервера."""
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Закрывает контексты сессии и транспорта; безопасен для повторного вызова."""
        session_ctx = self._session_ctx
        self._session_ctx = None
        if session_ctx is not None:
            await session_ctx.__aexit__(None, None, None)

        stdio_ctx = self._stdio_ctx
        self._stdio_ctx = None
        if stdio_ctx is not None:
            await stdio_ctx.__aexit__(None, None, None)

        self._session = None
        self._initialize_result = None

    def _ensure_connected(self) -> None:
        if not self.is_connected:
            raise ConnectionError("Нет активного MCP-соединения. Сначала выполните подключение.")
