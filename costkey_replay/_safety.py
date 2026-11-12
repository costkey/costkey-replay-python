"""The recorder rides the agent's own browser objects, so a bug here must never
break the agent. Every hook body runs through ``guard``; the first unexpected
error turns recording off for the rest of the process and is reported once."""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger("costkey_replay")

_lock = threading.Lock()
_disabled_reason: Optional[str] = None
_debug = False


def set_debug(enabled: bool) -> None:
    global _debug
    _debug = enabled


def debug(message: str, detail: object = None) -> None:
    if not _debug:
        return
    if detail is None:
        logger.warning("[costkey-replay] %s", message)
    else:
        logger.warning("[costkey-replay] %s: %s", message, detail)


def is_disabled() -> bool:
    return _disabled_reason is not None


def disabled_because() -> Optional[str]:
    return _disabled_reason


def disable(reason: str, error: Optional[BaseException] = None) -> None:
    global _disabled_reason
    with _lock:
        if _disabled_reason is not None:
            return
        _disabled_reason = reason
    detail = f": {error}" if error is not None else ""
    logger.warning("[costkey-replay] Recording stopped (%s%s). Your agent is unaffected.", reason, detail)


def guard(fn: Callable[[], T], fallback: Optional[T] = None) -> Optional[T]:
    """Run a hook body; on error, stop recording and return the fallback."""
    if _disabled_reason is not None:
        return fallback
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - nothing may escape into the agent
        disable("internal error", error)
        return fallback


def reset() -> None:
    """Test helper: start over."""
    global _disabled_reason
    with _lock:
        _disabled_reason = None
