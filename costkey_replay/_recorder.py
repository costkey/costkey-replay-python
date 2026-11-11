"""One page's recording: our own CDP session, rrweb in an isolated world, chunks to the uploader."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Pattern, Protocol, Sequence, Set

from costkey_replay._inject import BINDING_NAME, WORLD_NAME, injected_script
from costkey_replay._safety import debug, guard, is_disabled
from costkey_replay._uploader import Uploader


class CdpLike(Protocol):
    """The slice of a CDP session the recorder needs. The recorder always gets
    its own session on the page, so the agent's sessions never see our commands."""

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any: ...

    def on(self, event: str, handler: Callable[[Any], None]) -> Any: ...


#: rrweb custom event tags the console understands.
ACTION_TAG = "costkey.action"
ASSET_TAG = "costkey.asset"

RRWEB_CUSTOM = 5
MAX_ASSET_BYTES = 2 * 1024 * 1024
ASSET_TYPES = frozenset({"Image", "Font"})
ASSET_MEDIA = re.compile(
    r"^(image/(png|jpeg|gif|webp|avif|svg\+xml|x-icon|vnd\.microsoft\.icon)"
    r"|font/(woff2?|ttf|otf|sfnt)"
    r"|application/(font-woff2?|x-font-(woff|ttf|otf)|vnd\.ms-fontobject))$"
)
_HTTP = re.compile(r"^https?://")

Spawn = Callable[[Awaitable[Any]], None]


def now_ms() -> int:
    return int(time.time() * 1000)


class PageRecorder:
    def __init__(
        self,
        cdp: CdpLike,
        *,
        uploader: Uploader,
        spawn: Spawn,
        block_urls: Sequence[Pattern[str]] = (),
        trace_id: Optional[str] = None,
        max_chunk_bytes: int = 1024 * 1024,
        max_asset_bytes: int = 50 * 1024 * 1024,
        script: Optional[str] = None,
    ) -> None:
        self.recording_id = str(uuid.uuid4())
        self.trace_id = trace_id
        self._cdp = cdp
        self._uploader = uploader
        self._spawn = spawn
        self._block_urls = list(block_urls)
        self._max_chunk_bytes = max_chunk_bytes
        self._max_asset_bytes = max_asset_bytes
        self._script = script
        self._lock = threading.RLock()
        self._events: List[Any] = []
        self._bytes = 0
        self._seq = 0
        self._stopped = False
        self._blocked = False
        self._context_id: Optional[int] = None
        self._asset_bytes = 0
        self._pending_assets: Dict[str, Dict[str, str]] = {}
        self._asset_refs: Set[str] = set()

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    async def start(self) -> None:
        """Inject the recorder into new documents and the one already loaded. Raises if the page went away."""
        cdp = self._cdp
        cdp.on("Runtime.bindingCalled", lambda event: guard(lambda: self._on_binding(event)))
        cdp.on("Network.responseReceived", lambda event: guard(lambda: self._on_response(event)))
        cdp.on("Network.loadingFinished", lambda event: guard(lambda: self._on_loaded(event)))
        cdp.on("Network.loadingFailed", lambda event: guard(lambda: self._pending_assets.pop(str((event or {}).get("requestId")), None)))

        script = self._script if self._script is not None else injected_script()
        await cdp.send("Runtime.enable")
        await cdp.send("Runtime.addBinding", {"name": BINDING_NAME, "executionContextName": WORLD_NAME})
        await cdp.send("Page.enable")
        await cdp.send("Network.enable", {"maxResourceBufferSize": 4 * 1024 * 1024, "maxTotalBufferSize": 32 * 1024 * 1024})
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": script, "worldName": WORLD_NAME})

        # The document already showing when we attached.
        tree = await cdp.send("Page.getFrameTree")
        frame_id = ((tree or {}).get("frameTree") or {}).get("frame", {}).get("id")
        if isinstance(frame_id, str):
            try:
                world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD_NAME})
                await cdp.send("Runtime.evaluate", {"expression": script, "contextId": (world or {}).get("executionContextId")})
            except Exception as error:  # noqa: BLE001 - it navigated meanwhile; the new document gets the script anyway
                debug("couldn't record the current document", error)
        await self._cache_loaded_assets()

    async def _cache_loaded_assets(self) -> None:
        """Images and fonts that finished loading before our Network events were
        on are still in the page's resource tree; read them from there."""
        try:
            tree = await self._cdp.send("Page.getResourceTree")
        except Exception:  # noqa: BLE001
            return
        wanted: List[Dict[str, str]] = []

        def walk(node: Any) -> None:
            frame_id = ((node or {}).get("frame") or {}).get("id")
            if not frame_id:
                return
            for resource in node.get("resources") or []:
                media_type = str(resource.get("mimeType") or "").lower()
                url = str(resource.get("url") or "")
                if str(resource.get("type")) in ASSET_TYPES and _HTTP.match(url) and ASSET_MEDIA.match(media_type) and not resource.get("failed"):
                    wanted.append({"frameId": frame_id, "url": url, "mediaType": media_type})
            for child in node.get("childFrames") or []:
                walk(child)

        walk((tree or {}).get("frameTree"))
        for asset in wanted[:200]:
            if self._stopped or self._asset_bytes >= self._max_asset_bytes:
                return
            try:
                content = await self._cdp.send("Page.getResourceContent", {"frameId": asset["frameId"], "url": asset["url"]})
            except Exception:  # noqa: BLE001 - gone from the cache: blank in the replay
                continue
            guard(lambda: self._keep_asset(asset, (content or {}).get("content"), bool((content or {}).get("base64Encoded"))))

    def set_trace(self, trace_id: Optional[str]) -> None:
        """A later action ran under another run: close this run's chunk and start the next with a full snapshot."""
        if not trace_id or trace_id == self.trace_id or self._stopped:
            return
        with self._lock:
            self._flush_locked()
            self.trace_id = trace_id
        self._request_snapshot()

    def add_action(self, action: Dict[str, Any], at: Optional[int] = None) -> None:
        if self._stopped or self._blocked:
            return
        self._push({"type": RRWEB_CUSTOM, "timestamp": at if at is not None else now_ms(), "data": {"tag": ACTION_TAG, "payload": action}})

    def flush(self) -> None:
        """Send what's buffered as the next chunk."""
        with self._lock:
            self._flush_locked()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._flush_locked()
            self._stopped = True

    def _flush_locked(self) -> None:
        if not self._events:
            return
        events, self._events, self._bytes = self._events, [], 0
        seq = self._seq
        self._seq += 1
        self._uploader.enqueue_chunk(self.recording_id, seq, self.trace_id, events)

    def _push(self, event: Any, size: int = 200) -> None:
        with self._lock:
            if self._stopped:
                return
            self._events.append(event)
            self._bytes += size
            if self._bytes >= self._max_chunk_bytes:
                self._flush_locked()

    def _request_snapshot(self) -> None:
        context_id = self._context_id
        if context_id is None:
            return

        async def snapshot() -> None:
            try:
                await self._cdp.send("Runtime.evaluate", {
                    "expression": "globalThis.__ckReplay && globalThis.__ckReplay.snapshot && globalThis.__ckReplay.snapshot()",
                    "contextId": context_id,
                })
            except Exception:  # noqa: BLE001
                pass

        self._spawn(snapshot())

    def _on_binding(self, event: Any) -> None:
        if not isinstance(event, dict) or event.get("name") != BINDING_NAME or self._stopped or is_disabled():
            return
        if isinstance(event.get("executionContextId"), int):
            self._context_id = event["executionContextId"]
        payload = event.get("payload") if isinstance(event.get("payload"), str) else ""
        batch = json.loads(payload)
        href = batch.get("href") if isinstance(batch, dict) and isinstance(batch.get("href"), str) else ""
        if any(pattern.match(href) for pattern in self._block_urls):
            self._blocked = True
            return
        if self._blocked:
            # Left a blocked page without a new document: start again from a snapshot.
            self._blocked = False
            self._request_snapshot()
            return
        events = batch.get("events") if isinstance(batch, dict) else None
        if not isinstance(events, list) or not events:
            return
        share = -(-len(payload) // max(1, len(events)))
        for item in events:
            self._push(item, share)

    def _on_response(self, event: Any) -> None:
        if self._stopped or not isinstance(event, dict) or not event.get("requestId") or str(event.get("type")) not in ASSET_TYPES:
            return
        response = event.get("response") or {}
        url = str(response.get("url") or "")
        media_type = str(response.get("mimeType") or "").split(";")[0].strip().lower()
        if response.get("status") != 200 or not _HTTP.match(url) or not ASSET_MEDIA.match(media_type):
            return
        if self._blocked or self._asset_bytes >= self._max_asset_bytes:
            return
        self._pending_assets[str(event["requestId"])] = {"url": url, "mediaType": media_type}

    def _on_loaded(self, event: Any) -> None:
        request_id = str((event or {}).get("requestId") or "")
        pending = self._pending_assets.pop(request_id, None)
        if not pending:
            return
        if ((event or {}).get("encodedDataLength") or 0) > MAX_ASSET_BYTES * 2:
            return

        async def fetch() -> None:
            try:
                result = await self._cdp.send("Network.getResponseBody", {"requestId": request_id})
            except Exception:  # noqa: BLE001 - evicted already: that spot shows blank in the replay
                return
            guard(lambda: self._keep_asset(pending, (result or {}).get("body"), bool((result or {}).get("base64Encoded"))))

        self._spawn(fetch())

    def _keep_asset(self, asset: Dict[str, str], body: Any, base64_encoded: bool) -> None:
        if self._stopped or not isinstance(body, str):
            return
        data = base64.b64decode(body) if base64_encoded else body.encode("utf-8")
        if not data or len(data) > MAX_ASSET_BYTES or self._asset_bytes + len(data) > self._max_asset_bytes:
            return
        sha256 = hashlib.sha256(data).hexdigest()
        ref = f"{asset['url']} {sha256}"
        if ref in self._asset_refs:
            return
        self._asset_refs.add(ref)
        self._asset_bytes += len(data)
        self._uploader.enqueue_asset(sha256, asset["mediaType"], data)
        self._push({"type": RRWEB_CUSTOM, "timestamp": now_ms(), "data": {"tag": ASSET_TAG, "payload": {"url": asset["url"], "sha256": sha256, "mediaType": asset["mediaType"]}}})


def compile_block_list(patterns: Optional[Sequence[str]]) -> List[Pattern[str]]:
    """``["*/billing/*"]`` to regular expressions matched against the whole URL."""
    compiled = []
    for pattern in patterns or ():
        if isinstance(pattern, str) and pattern:
            compiled.append(re.compile("^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$"))
    return compiled
