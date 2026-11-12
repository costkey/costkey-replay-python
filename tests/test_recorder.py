import asyncio
import base64
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from costkey_replay._inject import BINDING_NAME, WORLD_NAME
from costkey_replay._playwright import _action
from costkey_replay._recorder import ACTION_TAG, ASSET_TAG, PageRecorder, compile_block_list


class FakeCdp:
    def __init__(self) -> None:
        self.sent: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        self.handlers: Dict[str, Callable[[Any], None]] = {}

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.sent.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame_1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Page.getResourceTree":
            return {"frameTree": {"frame": {"id": "frame_1"}, "resources": [{"url": "https://shop.example.com/logo.png", "type": "Image", "mimeType": "image/png"}]}}
        if method in ("Page.getResourceContent", "Network.getResponseBody"):
            return {"content": base64.b64encode(b"png").decode(), "body": base64.b64encode(b"png2").decode(), "base64Encoded": True}
        return {}

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers[event] = handler

    def emit(self, event: str, params: Any) -> None:
        self.handlers[event](params)


class FakeUploader:
    stopped = False

    def __init__(self) -> None:
        self.chunks: List[Dict[str, Any]] = []
        self.assets: List[Tuple[str, str, bytes]] = []

    def enqueue_chunk(self, recording_id: str, seq: int, trace_id: Optional[str], events: List[Any]) -> None:
        self.chunks.append({"recordingId": recording_id, "seq": seq, "traceId": trace_id, "events": events})

    def enqueue_asset(self, sha256: str, media_type: str, data: bytes) -> None:
        self.assets.append((sha256, media_type, data))


def make(**kwargs: Any) -> Tuple[PageRecorder, FakeCdp, FakeUploader, List[Any]]:
    cdp, uploader, spawned = FakeCdp(), FakeUploader(), []
    recorder = PageRecorder(cdp, uploader=uploader, spawn=spawned.append, script="/* rrweb */", **kwargs)  # type: ignore[arg-type]
    return recorder, cdp, uploader, spawned


def binding(href: str, events: List[Any]) -> Dict[str, Any]:
    return {"name": BINDING_NAME, "executionContextId": 7, "payload": json.dumps({"href": href, "events": events})}


def test_start_injects_into_an_isolated_world_and_the_current_document():
    recorder, cdp, uploader, _ = make()
    asyncio.run(recorder.start())
    methods = [method for method, _ in cdp.sent]
    assert methods[:5] == ["Runtime.enable", "Runtime.addBinding", "Page.enable", "Network.enable", "Page.addScriptToEvaluateOnNewDocument"]
    assert dict(cdp.sent)["Runtime.addBinding"] == {"name": BINDING_NAME, "executionContextName": WORLD_NAME}
    assert dict(cdp.sent)["Runtime.evaluate"] == {"expression": "/* rrweb */", "contextId": 7}
    # The image already in the page's cache is kept, by hash.
    assert uploader.assets[0][1:] == ("image/png", b"png")


def test_events_and_actions_become_numbered_chunks_for_the_run():
    recorder, cdp, uploader, spawned = make(trace_id="trace_1")
    asyncio.run(recorder.start())
    cdp.emit("Runtime.bindingCalled", binding("https://shop.example.com/cart", [{"type": 4, "timestamp": 1}, {"type": 2, "timestamp": 2}]))
    recorder.add_action({"name": "page.click", "selector": "#buy"})
    recorder.flush()
    recorder.set_trace("trace_2")
    cdp.emit("Runtime.bindingCalled", binding("https://shop.example.com/cart", [{"type": 3, "timestamp": 3}]))
    recorder.stop()
    chunks = uploader.chunks
    # Everything before the run changed is one chunk; the new run starts the next.
    assert [(chunk["seq"], chunk["traceId"]) for chunk in chunks] == [(0, "trace_1"), (1, "trace_2")]
    first = [event.get("data", {}).get("payload") for event in chunks[0]["events"] if event.get("data", {}).get("tag") == ACTION_TAG]
    assert first == [{"name": "page.click", "selector": "#buy"}]
    assert [event["type"] for event in chunks[1]["events"]] == [3]
    for task in spawned:
        asyncio.run(task)
    assert len({chunk["recordingId"] for chunk in chunks}) == 1


def test_a_new_run_asks_the_page_for_a_full_snapshot():
    recorder, cdp, uploader, spawned = make(trace_id="trace_1")
    asyncio.run(recorder.start())
    cdp.emit("Runtime.bindingCalled", binding("https://shop.example.com/", [{"type": 4}]))
    recorder.set_trace("trace_2")
    assert len(spawned) == 1
    asyncio.run(spawned[0])
    assert cdp.sent[-1][0] == "Runtime.evaluate" and "snapshot()" in cdp.sent[-1][1]["expression"]


def test_blocked_pages_keep_nothing():
    recorder, cdp, uploader, spawned = make(block_urls=compile_block_list(["*/billing/*"]))
    asyncio.run(recorder.start())
    cdp.emit("Runtime.bindingCalled", binding("https://shop.example.com/billing/card", [{"type": 2}]))
    recorder.add_action({"name": "page.fill", "selector": "#card", "textLength": 16})
    recorder.flush()
    # Only the logo cached before the recorder knew where it was; nothing from the blocked page.
    kept = [event for chunk in uploader.chunks for event in chunk["events"]]
    assert all(event.get("data", {}).get("tag") == ASSET_TAG for event in kept)
    # Leaving the blocked page starts again from a snapshot.
    cdp.emit("Runtime.bindingCalled", binding("https://shop.example.com/cart", [{"type": 3}]))
    assert len(spawned) == 1
    asyncio.run(spawned[0])


def test_images_from_the_network_are_uploaded_once():
    recorder, cdp, uploader, spawned = make()
    asyncio.run(recorder.start())
    for request_id in ("r1", "r2"):
        cdp.emit("Network.responseReceived", {"requestId": request_id, "type": "Image", "response": {"url": "https://shop.example.com/a.png", "status": 200, "mimeType": "image/png"}})
        cdp.emit("Network.loadingFinished", {"requestId": request_id, "encodedDataLength": 10})
    for task in spawned:
        asyncio.run(task)
    assert [asset[2] for asset in uploader.assets].count(b"png2") == 1
    recorder.flush()
    tags = [event["data"]["tag"] for chunk in uploader.chunks for event in chunk["events"]]
    assert tags.count(ASSET_TAG) == 2  # The cached logo and the network image.


def test_typed_text_is_never_kept():
    action = _action("locator", "Frame", "fill", {"selector": "#email", "value": "agent@example.com"})
    assert action == {"name": "locator.fill", "selector": "#email", "textLength": 17}
    assert _action("keyboard", "Page", "keyboardPress", {"key": "Enter"}) == {"name": "keyboard.keyboardPress", "key": "Enter"}
    assert _action("keyboard", "Page", "keyboardPress", {"key": "a"}) == {"name": "keyboard.keyboardPress", "textLength": 1}
    assert _action(None, "Page", "mouseClick", {"x": 10.04, "y": 20}) == {"name": "page.mouseClick", "x": 10.0, "y": 20.0}
