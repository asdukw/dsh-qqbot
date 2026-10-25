"""Local correlation for message handling, including asyncio worker threads."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from contextvars import ContextVar
from functools import wraps
from typing import Any
from uuid import uuid4

_request_id: ContextVar[str] = ContextVar("qqbot_request_id", default="-")


class RequestLogger(logging.LoggerAdapter):
    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        return f"rid={_request_id.get()} {msg}", kwargs


def get_logger(name: str) -> RequestLogger:
    return RequestLogger(logging.getLogger(name), {})


def request_context[**P, T](func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    """Reuse a nested turn's ID; reset even on failure or cancellation."""
    @wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        if _request_id.get() != "-":
            return await func(*args, **kwargs)
        token = _request_id.set(uuid4().hex[:12])
        try:
            return await func(*args, **kwargs)
        finally:
            _request_id.reset(token)

    return wrapped
