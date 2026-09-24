"""Пакет MCP-клиента.

Содержит низкоуровневый транспортный слой (:mod:`client.session`) и
высокоуровневую логику (:mod:`client.logic`), отделённые друг от друга для
возможности дальнейшего расширения (автономный tool use, отложенное выполнение
и т. д.).
"""

from client.logic import MCPApp, StatusInfo, ToolInfo, create_app, format_tools
from client.session import MCPSession

__all__ = [
    "MCPSession",
    "MCPApp",
    "StatusInfo",
    "ToolInfo",
    "create_app",
    "format_tools",
]
