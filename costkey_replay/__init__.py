"""Session replay for Python browser agents.

``costkey_replay.init()`` patches the Playwright your app already uses: you keep
using it as before, and each Chromium page gets an rrweb recording linked to
the CostKey run it happens in. Nothing records until the project turns on
screen recordings (Settings, Privacy & data).
"""
from __future__ import annotations

import atexit
import os
import threading
from typing import List, Optional, Sequence

from costkey_replay import _trace
from costkey_replay._config import fetch_screen_capture_enabled, parse_dsn
from costkey_replay._playwright import hook_playwright
from costkey_replay._recorder import compile_block_list
from costkey_replay._safety import debug as log_debug, disable, set_debug
from costkey_replay._session import ReplaySession
from costkey_replay._uploader import Uploader
from costkey_replay.version import __version__

__all__ = ["init", "flush", "shutdown", "__version__"]

#: How often buffered events go to the uploader.
FLUSH_INTERVAL_S = 2.0
#: How often the project setting is checked again while the process runs.
CONFIG_REFRESH_S = 300.0


class _State:
    def __init__(self, session: ReplaySession) -> None:
        self.session = session
        self.stop = threading.Event()


_state: Optional[_State] = None
_lock = threading.Lock()


def init(dsn: Optional[str] = None, *, block_urls: Optional[Sequence[str]] = None, debug: Optional[bool] = None) -> None:
    """Start recording the browser pages your agent drives. Call it before launching the browser.

    ``dsn`` defaults to ``COSTKEY_DSN``, the same DSN the core SDK takes.
    ``block_urls`` are URL globs not to record, such as ``["*/billing/*"]``
    (default ``COSTKEY_REPLAY_BLOCK_URLS``, comma-separated); while a matching
    page shows, nothing from it is kept. ``debug`` defaults to ``COSTKEY_REPLAY_DEBUG=true``.
    """
    global _state
    with _lock:
        if _state is not None:
            return
        dsn_string = dsn or os.environ.get("COSTKEY_DSN")
        if not dsn_string:
            return  # No DSN (tests, local runs): do nothing.
        set_debug(debug if debug is not None else os.environ.get("COSTKEY_REPLAY_DEBUG") == "true")
        if block_urls is None:
            block_urls = [part.strip() for part in os.environ.get("COSTKEY_REPLAY_BLOCK_URLS", "").split(",") if part.strip()]
        parsed = parse_dsn(dsn_string)

        session_ref: List[ReplaySession] = []

        def rejected(reason: str) -> None:
            for recorder in session_ref[0].clear_live() if session_ref else []:
                recorder.stop()
            disable(reason)

        session = ReplaySession(Uploader(parsed, on_rejected=rejected), compile_block_list(block_urls))
        session_ref.append(session)
        state = _State(session)

        def check_setting() -> None:
            while not state.stop.is_set():
                allowed = fetch_screen_capture_enabled(parsed)
                if allowed is not None:
                    session.set_allowed(allowed)
                    if not allowed:
                        log_debug("screen recordings are off for this project; not recording")
                state.stop.wait(CONFIG_REFRESH_S if allowed is not None else 15.0)

        def flush_often() -> None:
            while not state.stop.wait(FLUSH_INTERVAL_S):
                for recorder in session.live():
                    recorder.flush()

        threading.Thread(target=check_setting, name="costkey-replay-config", daemon=True).start()
        threading.Thread(target=flush_often, name="costkey-replay-flush", daemon=True).start()
        if not _trace.load():
            log_debug("costkey SDK not found: recordings won't be linked to runs")
        try:
            if not hook_playwright(session):
                log_debug("Playwright not found: nothing to record")
        except Exception as error:  # noqa: BLE001
            disable("couldn't start", error)
        _state = state
    atexit.register(_at_exit)


def flush(timeout: float = 10.0) -> bool:
    """Send everything recorded so far. Call it before a short-lived process exits. False on timeout."""
    state = _state
    if state is None:
        return True
    for recorder in state.session.live():
        recorder.flush()
    return state.session.uploader.flush(timeout)


def shutdown(timeout: float = 10.0) -> None:
    """Stop recording and send what's left."""
    global _state
    with _lock:
        state, _state = _state, None
    if state is None:
        return
    state.stop.set()
    for recorder in state.session.clear_live():
        recorder.stop()
    state.session.uploader.flush(timeout)


def _at_exit() -> None:
    try:
        flush(5.0)
    except Exception:  # noqa: BLE001
        pass
