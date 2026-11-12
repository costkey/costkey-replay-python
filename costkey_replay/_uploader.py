"""Sends chunks and assets on a background thread, one request at a time, so a
slow or unreachable server costs memory up to a bound and never time on the
agent's side."""
from __future__ import annotations

import gzip
import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

from costkey_replay._config import Dsn, send
from costkey_replay._safety import debug
from costkey_replay.version import __version__

Request = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, str]]

DEFAULT_MAX_QUEUE_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 4
#: Hashes this process already uploaded or found on the server.
MAX_REMEMBERED_ASSETS = 8192


@dataclass
class _Chunk:
    body: bytes
    attempts: int = 0
    last_chance: bool = False

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class _Asset:
    sha256: str
    media_type: str
    data: bytes
    attempts: int = 0
    last_chance: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


_Job = Union[_Chunk, _Asset]


@dataclass
class _State:
    queue: Deque[_Job] = field(default_factory=deque)
    queued_bytes: int = 0
    busy: bool = False


class Uploader:
    def __init__(
        self,
        dsn: Dsn,
        *,
        max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES,
        on_rejected: Optional[Callable[[str], None]] = None,
        request: Optional[Request] = None,
        retry_delay: Callable[[int], float] = lambda attempt: min(30.0, float(2 ** attempt)),
    ) -> None:
        self._dsn = dsn
        self._max_queue_bytes = max_queue_bytes
        self._on_rejected = on_rejected
        self._request: Request = request or (lambda method, url, headers, body: send(method, url, headers, body))
        self._retry_delay = retry_delay
        self._state = _State()
        self._known_assets: "OrderedDict[str, None]" = OrderedDict()
        self._rejected = False
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._retry_at = 0.0
        self._final = False

    @property
    def stopped(self) -> bool:
        return self._rejected

    def enqueue_chunk(self, recording_id: str, seq: int, trace_id: Optional[str], events: List[Any]) -> None:
        if self._rejected or not events:
            return
        body = gzip.compress(
            json.dumps({"recordingId": recording_id, "seq": seq, "traceId": trace_id, "events": events}, separators=(",", ":")).encode("utf-8"),
            compresslevel=6,
        )
        with self._cond:
            if not self._make_room(len(body)):
                debug(f"dropped chunk {recording_id}#{seq}: upload queue full")
                return
            self._push(_Chunk(body))

    def enqueue_asset(self, sha256: str, media_type: str, data: bytes) -> None:
        """Queue an asset once per process; the server skips it if the project has it already."""
        with self._cond:
            if self._rejected or sha256 in self._known_assets:
                return
            self._remember(sha256)
            if self._state.queued_bytes + len(data) > self._max_queue_bytes:
                self._known_assets.pop(sha256, None)
                debug(f"dropped asset {sha256}: upload queue full")
                return
            self._push(_Asset(sha256, media_type, data))

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued so far is sent or given up on. Uploads
        waiting on a retry get one more try now. False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._final = True
            self._retry_at = 0.0
            if self._state.queue and self._state.queue[0].attempts > 0:
                self._state.queue[0].last_chance = True  # Waiting on a retry: this is its one more try.
            self._ensure_thread()
            self._cond.notify_all()
            try:
                while (self._state.queue or self._state.busy) and not self._rejected:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cond.wait(min(left, 0.2))
            finally:
                self._final = False
        return True

    # Queue (callers hold the lock)

    def _push(self, job: _Job) -> None:
        self._state.queue.append(job)
        self._state.queued_bytes += job.size
        self._ensure_thread()
        self._cond.notify_all()

    def _ensure_thread(self) -> None:
        if self._state.queue and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="costkey-replay-uploader", daemon=True)
            self._thread.start()

    def _make_room(self, size: int) -> bool:
        """Free space by dropping queued assets (cheapest to lose) before refusing new data."""
        state = self._state
        if state.queued_bytes + size <= self._max_queue_bytes:
            return True
        for job in reversed(list(state.queue)):
            if state.queued_bytes + size <= self._max_queue_bytes:
                break
            if isinstance(job, _Asset):
                state.queue.remove(job)
                state.queued_bytes -= job.size
                self._known_assets.pop(job.sha256, None)
        return state.queued_bytes + size <= self._max_queue_bytes

    def _remember(self, sha256: str) -> None:
        if len(self._known_assets) >= MAX_REMEMBERED_ASSETS:
            self._known_assets.popitem(last=False)
        self._known_assets[sha256] = None

    # Worker

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._rejected:
                        return
                    if self._state.queue and time.monotonic() >= self._retry_at:
                        break
                    wait = 30.0 if not self._state.queue else max(0.05, self._retry_at - time.monotonic())
                    if not self._cond.wait(wait) and not self._state.queue:
                        self._thread = None  # Idle: the next push starts a new thread.
                        return
                job = self._state.queue[0]
                self._state.busy = True
            outcome = self._send(job)
            with self._cond:
                self._state.busy = False
                if self._rejected:
                    self._cond.notify_all()
                    return
                if outcome == "retry" and job.attempts < MAX_ATTEMPTS and not (self._final and job.last_chance):
                    job.attempts += 1
                    if self._final:
                        job.last_chance = True  # Flushing: one more try right away, then give up.
                        self._retry_at = 0.0
                    else:
                        self._retry_at = time.monotonic() + self._retry_delay(job.attempts)
                else:
                    if self._state.queue and self._state.queue[0] is job:
                        self._state.queue.popleft()
                        self._state.queued_bytes -= job.size
                    if outcome != "done" and isinstance(job, _Asset):
                        self._known_assets.pop(job.sha256, None)
                    self._retry_at = 0.0
                self._cond.notify_all()

    def _headers(self, extra: Dict[str, str]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._dsn.auth_key}", "User-Agent": f"costkey-replay-python/{__version__}", **extra}

    def _send(self, job: _Job) -> str:
        try:
            if isinstance(job, _Chunk):
                status, text = self._request(
                    "POST",
                    f"{self._dsn.base_url}/api/v1/replay/chunks",
                    self._headers({"Content-Type": "application/json", "Content-Encoding": "gzip"}),
                    job.body,
                )
                return self._outcome(status, text, "chunk")
            url = f"{self._dsn.base_url}/api/v1/replay/assets/{job.sha256}"
            status, text = self._request("HEAD", url, self._headers({}), None)
            if status == 200:
                return "done"
            if status != 404:
                return self._outcome(status, text, "asset")
            status, text = self._request("PUT", url, self._headers({"Content-Type": job.media_type}), job.data)
            return self._outcome(status, text, "asset")
        except Exception as error:  # noqa: BLE001
            debug("upload failed, will retry", error)
            return "retry"

    def _outcome(self, status: int, text: str, what: str) -> str:
        if 200 <= status < 300:
            return "done"
        if status == 422:
            return self._reject("screen recordings are off for this project")
        if status in (401, 403):
            return self._reject("the DSN was rejected")
        if status in (404, 405, 501):
            return self._reject("this CostKey server doesn't accept replays yet")
        if status == 429 or status >= 500:
            return "retry"
        debug(f"server refused {what} ({status})", text[:200])
        return "drop"

    def _reject(self, reason: str) -> str:
        callback = None
        with self._cond:
            if not self._rejected:
                self._rejected = True
                self._state.queue.clear()
                self._state.queued_bytes = 0
                callback = self._on_rejected
            self._cond.notify_all()
        if callback:
            try:
                callback(reason)
            except Exception:  # noqa: BLE001
                pass
        return "drop"
