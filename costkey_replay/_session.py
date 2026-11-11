"""What the hooks share: whether recording is allowed, how to start a recorder,
and the page to recorder map used to tag actions with the page and run."""
from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Pattern, Set, Tuple

from costkey_replay._recorder import CdpLike, PageRecorder, Spawn, now_ms
from costkey_replay._safety import debug, is_disabled
from costkey_replay._uploader import Uploader

MAX_PENDING_ACTIONS = 50


@dataclass
class _Pending:
    trace_id: Optional[str] = None
    actions: List[Tuple[Dict[str, Any], int]] = field(default_factory=list)


class ReplaySession:
    def __init__(self, uploader: Uploader, block_urls: List[Pattern[str]]) -> None:
        self.uploader = uploader
        self.block_urls = block_urls
        self.recorders: "weakref.WeakKeyDictionary[Any, PageRecorder]" = weakref.WeakKeyDictionary()
        self.pending: "weakref.WeakKeyDictionary[Any, _Pending]" = weakref.WeakKeyDictionary()
        self.claimed: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._live: Set[PageRecorder] = set()
        self._live_lock = threading.Lock()
        self._allowed: Optional[bool] = None
        self._allowed_known = threading.Event()

    # The project's screen-recording setting (the remote half of the opt-in)

    def set_allowed(self, allowed: bool) -> None:
        self._allowed = allowed
        self._allowed_known.set()

    def allowed_now(self) -> Optional[bool]:
        return self._allowed

    def wait_allowed(self, timeout: float) -> bool:
        self._allowed_known.wait(timeout)
        return self._allowed is True

    # Live recorders, also read by the flush thread

    def add_live(self, recorder: PageRecorder) -> None:
        with self._live_lock:
            self._live.add(recorder)

    def remove_live(self, recorder: PageRecorder) -> None:
        with self._live_lock:
            self._live.discard(recorder)

    def live(self) -> List[PageRecorder]:
        with self._live_lock:
            return list(self._live)

    def clear_live(self) -> List[PageRecorder]:
        with self._live_lock:
            live, self._live = list(self._live), set()
        return live


async def record_page(
    session: ReplaySession,
    page: Any,
    open_cdp: Callable[[], Awaitable[CdpLike]],
    trace_id: Optional[str],
    on_close: Callable[[Callable[[], None]], None],
    spawn: Spawn,
) -> Optional[PageRecorder]:
    """Start recording a page on a CDP session of our own. Never raises."""
    if is_disabled() or session.uploader.stopped or page in session.claimed:
        return None
    session.claimed.add(page)
    allowed = session.allowed_now()
    if allowed is None:
        # The setting is still on its way: wait for it off the event loop.
        allowed = await asyncio.get_running_loop().run_in_executor(None, session.wait_allowed, 10.0)
    if not allowed:
        return None
    try:
        cdp = await open_cdp()
    except Exception as error:  # noqa: BLE001 - not Chromium, or the page closed first
        debug("no CDP session for page", error)
        return None
    early = session.pending.pop(page, None)
    recorder = PageRecorder(
        cdp,
        uploader=session.uploader,
        spawn=spawn,
        block_urls=session.block_urls,
        trace_id=(early.trace_id if early else None) or trace_id,
    )
    for action, at in early.actions if early else []:
        recorder.add_action(action, at)
    session.recorders[page] = recorder
    session.add_live(recorder)

    def stop() -> None:
        recorder.stop()
        session.remove_live(recorder)

    on_close(stop)
    try:
        await recorder.start()
    except Exception as error:  # noqa: BLE001
        debug("page went away while starting the recorder", error)
        stop()
        return None
    return recorder


def note_action(session: ReplaySession, page: Any, trace_id: Optional[str], action: Optional[Dict[str, Any]] = None) -> None:
    if page is None:
        return
    recorder = session.recorders.get(page)
    if recorder is not None:
        if recorder.is_stopped:
            return
        recorder.set_trace(trace_id)
        if action:
            recorder.add_action(action)
        return
    early = session.pending.get(page)
    if early is None:
        early = _Pending()
        session.pending[page] = early
    if trace_id:
        early.trace_id = trace_id
    if action and len(early.actions) < MAX_PENDING_ACTIONS:
        early.actions.append((action, now_ms()))


def key_name(value: Any) -> Optional[str]:
    """Named keys (Enter, Tab, Control+A) are kept; a single character is typed text, so it isn't."""
    if not isinstance(value, str) or not value:
        return None
    key = value.split("+")[-1]
    return clip(value, 40) if len(key) > 1 else None


def masked_length(value: Any) -> Optional[int]:
    """Typed text never leaves the process: keep its length only."""
    if isinstance(value, (str, list, tuple)):
        return len(value)
    return None


def clip(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit] + "…" if len(value) > limit else value


def finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(float(value) * 10) / 10
