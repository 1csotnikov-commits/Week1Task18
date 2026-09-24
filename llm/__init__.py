"""Пакет для интеграции с LLM (DeepSeek)."""

from llm.provider import LLMError, ask, get_api_key

__all__ = ["ask", "get_api_key", "LLMError"]
