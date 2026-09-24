"""MCP-сервер, предоставляющий инструменты для работы с локальным Git-репозиторием.

Транспорт — stdio. Сервер запускается как подпроцесс MCP-клиентом
(`python mcp_server.py`) и обменивается с ним JSON-RPC сообщениями через
stdin/stdout. Поэтому здесь запрещено писать что-либо в stdout (выводом
пользуется транспорт); логи идут в stderr.

Регистрация нового инструмента сводится к добавлению функции с декоратором
``@server.tool()`` — это одна из точек расширения проекта.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

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


if __name__ == "__main__":
    # stdio — единственный используемый транспорт.
    server.run(transport="stdio")
