"""The CostKey run (trace) the agent is in, from the app's own ``costkey`` SDK.

Playwright calls run in the agent's context (asyncio tasks and the sync API's
greenlets both carry ContextVars), so reading the core SDK's context inside a
call tells us which run the call belongs to.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_getter: Optional[Callable[[], Dict[str, Any]]] = None
_looked = False


def load() -> bool:
    """Find ``costkey.get_current_context``. False when the core SDK isn't installed."""
    global _getter, _looked
    if _getter is not None:
        return True
    _looked = True
    try:
        import costkey  # noqa: PLC0415 - optional
    except Exception:  # noqa: BLE001
        return False
    getter = getattr(costkey, "get_current_context", None)
    if callable(getter):
        _getter = getter
        return True
    return False


def set_getter(getter: Optional[Callable[[], Dict[str, Any]]]) -> None:
    """Test helper."""
    global _getter, _looked
    _getter = getter
    _looked = True


def current_trace_id() -> Optional[str]:
    if _getter is None:
        if _looked:
            return None
        load()
        if _getter is None:
            return None
    try:
        context = _getter()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(context, dict):
        return None
    trace_id = context.get("traceId") or context.get("trace_id")
    return trace_id if isinstance(trace_id, str) and trace_id else None
