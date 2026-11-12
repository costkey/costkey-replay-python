"""Playwright for Python, sync and async API. Two hooks on Playwright's own client:

- ``BrowserContext._on_page`` runs for every page a context gets: from
  ``new_page``, popups, ``launch_persistent_context`` and the pages already open
  when ``connect_over_cdp`` attaches. Each page gets a CDP session of our own
  (``context.new_cdp_session``).
- Every call the agent makes passes through the connection's send method with
  the object it was made on, so we learn which page an action ran on and which
  CostKey run (trace) it ran in. The call itself is passed on untouched.

Both hooks exist in the Playwright 1.4x layout (``playwright._impl._connection``)
and the newer one ported from the Node client (``playwright._impl.connection``).
Firefox and WebKit contexts have no CDP, so ``new_cdp_session`` fails and they
are skipped.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import weakref
from typing import Any, Awaitable, Dict, Optional, Set

from costkey_replay._recorder import CdpLike, Spawn
from costkey_replay._safety import debug, guard
from costkey_replay._session import ReplaySession, clip, finite, key_name, masked_length, note_action, record_page
from costkey_replay._trace import current_trace_id

HOOKED = "__costkey_replay_hooked__"

ACTIONS: Dict[str, frozenset] = {
    "Frame": frozenset({"goto", "click", "dblclick", "tap", "fill", "type", "press", "check", "uncheck", "selectOption", "hover", "setInputFiles", "dragAndDrop", "focus"}),
    "ElementHandle": frozenset({"click", "dblclick", "tap", "fill", "type", "press", "check", "uncheck", "selectOption", "hover", "setInputFiles", "focus", "screenshot"}),
    "Page": frozenset({"goBack", "goForward", "reload", "mouseClick", "mouseWheel", "keyboardType", "keyboardPress", "keyboardInsertText", "screenshot", "expectScreenshot"}),
}


def _impl_class(module_names: tuple, name: str) -> Any:
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - not this layout, or Playwright isn't installed
            continue
        cls = getattr(module, name, None)
        if cls is not None:
            return cls
    return None


def hook_playwright(session: ReplaySession) -> bool:
    """Patch the installed Playwright. False when there is none, or its client has neither hook."""
    connection_cls = _impl_class(("playwright._impl.connection", "playwright._impl._connection"), "Connection")
    context_cls = _impl_class(("playwright._impl.browser_context", "playwright._impl._browser_context"), "BrowserContext")
    if connection_cls is None or context_cls is None:
        return False
    send_name = "send_message_to_server" if hasattr(connection_cls, "send_message_to_server") else "_send_message_to_server"
    if not hasattr(connection_cls, send_name) or not hasattr(context_cls, "_on_page"):
        debug("this Playwright version has neither hook; not recording it")
        return False
    if getattr(connection_cls, HOOKED, False):
        return True

    context_traces: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
    tasks: Set["asyncio.Task[Any]"] = set()

    original_send = getattr(connection_cls, send_name)

    def send(self: Any, *args: Any, **kwargs: Any) -> Any:
        guard(lambda: _observe(self, args, kwargs))
        return original_send(self, *args, **kwargs)

    def _observe(connection: Any, args: tuple, kwargs: Dict[str, Any]) -> None:
        owner = args[0] if args else kwargs.get("object")
        method = args[1] if len(args) > 1 else kwargs.get("method")
        params = args[2] if len(args) > 2 else kwargs.get("params")
        options = args[3] if len(args) > 3 else kwargs.get("options")
        if isinstance(options, dict) and options.get("internal"):
            return
        trace_id = current_trace_id()
        owner_type = getattr(owner, "_type", None)
        if trace_id and owner_type in ("BrowserContext", "Browser"):
            # New contexts and pages made here start in this run (browser.new_page makes both).
            context_traces[owner] = trace_id
        methods = ACTIONS.get(owner_type) if isinstance(owner_type, str) else None
        action = None
        if methods and method in methods:
            action = _action(_api_object(connection, options), owner_type, str(method), params if isinstance(params, dict) else {})
        if trace_id or action:
            note_action(session, page_of(owner), trace_id, action)

    original_on_page = context_cls._on_page

    def _on_page(self: Any, page: Any) -> None:
        original_on_page(self, page)
        guard(lambda: _watch(self, page))

    def _watch(context: Any, page: Any) -> None:
        loop = getattr(page, "_loop", None) or getattr(context, "_loop", None)
        if loop is None:
            return

        def spawn(coro: Awaitable[Any]) -> None:
            async def safe() -> None:
                try:
                    await coro
                except Exception as error:  # noqa: BLE001
                    debug("recorder task failed", error)

            task = loop.create_task(safe())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def open_cdp() -> CdpLike:
            return _Cdp(await context.new_cdp_session(page))

        def on_close(stop: Any) -> None:
            page.once("close", lambda *_: guard(stop))

        browser = getattr(context, "_browser", None)
        trace_id = context_traces.get(context) or (context_traces.get(browser) if browser is not None else None)
        spawn(record_page(session, page, open_cdp, trace_id, on_close, spawn))

    setattr(connection_cls, send_name, send)
    context_cls._on_page = _on_page
    setattr(connection_cls, HOOKED, True)
    return True


class _Cdp:
    """Playwright's own CDPSession, with one send signature for every version."""

    def __init__(self, session: Any) -> None:
        self._session = session
        try:
            self._with_timeout = "timeout" in inspect.signature(type(session).send).parameters
        except (TypeError, ValueError):
            self._with_timeout = False

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._with_timeout:
            return await self._session.send(method, params, None)
        return await self._session.send(method, params)

    def on(self, event: str, handler: Any) -> Any:
        return self._session.on(event, handler)


def _is_page(value: Any) -> bool:
    cls = type(value)
    return cls.__name__ == "Page" and cls.__module__.startswith("playwright._impl")


def page_of(owner: Any) -> Any:
    """The page for an API call owner: the page itself, a frame, or an element handle."""
    node = owner
    for _ in range(8):
        if node is None:
            return None
        if _is_page(node):
            return node
        page = getattr(node, "_page", None)
        if page is not None and _is_page(page):
            return page
        node = getattr(node, "_parent", None)
    return None


def _api_object(connection: Any, options: Any) -> Optional[str]:
    """The public object the agent called, such as "locator" for ``Locator.click``.

    The newer layout passes the API name with the call; the 1.4x layout keeps it
    in a ContextVar on the connection.
    """
    name = options.get("api_name") if isinstance(options, dict) else None
    if not name:
        zone = getattr(connection, "_api_zone", None)
        try:
            info = zone.get() if zone is not None and hasattr(zone, "get") else None
        except Exception:  # noqa: BLE001
            info = None
        name = info.get("apiName") if isinstance(info, dict) else None
    if not isinstance(name, str) or "." not in name:
        return None
    head = name.split(".", 1)[0]
    return f"{head[:1].lower()}{head[1:]}" if head.isidentifier() else None


def _action(api_object: Optional[str], owner_type: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    # The protocol method is the verb (camelCase, as the console expects); the API object gives context.
    action: Dict[str, Any] = {"name": f"{api_object or owner_type.lower()}.{method}"}
    position = params.get("position") if isinstance(params.get("position"), dict) else {}
    selector = clip(params.get("selector"), 300)
    url = clip(params.get("url"), 500)
    key = key_name(params.get("key"))
    button = clip(params.get("button"), 10)
    x = finite(_first(params.get("x"), position.get("x"), params.get("deltaX")))
    y = finite(_first(params.get("y"), position.get("y"), params.get("deltaY")))
    text_length = masked_length(_first(params.get("text"), params.get("value"), params.get("values")))
    if text_length is None and isinstance(params.get("key"), str) and not key:
        text_length = 1
    if selector:
        action["selector"] = selector
    if url:
        action["url"] = url
    if key:
        action["key"] = key
    if button:
        action["button"] = button
    if x is not None:
        action["x"] = x
    if y is not None:
        action["y"] = y
    if text_length is not None:
        action["textLength"] = text_length
    return action


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
