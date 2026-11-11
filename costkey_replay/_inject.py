"""The script that runs in each recorded page. The same text as @costkey/replay."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

#: The isolated world the recorder runs in: same DOM as the page, but the page's scripts can't see it.
WORLD_NAME = "__costkey_replay"
#: CDP binding the recorder calls with batches of events; exists only in that world.
BINDING_NAME = "__costkeyReplayEmit"

_bundle: Optional[str] = None


def recorder_bundle() -> str:
    """rrweb's recorder, vendored in the package. It runs in the page, never in Python."""
    global _bundle
    if _bundle is None:
        _bundle = (Path(__file__).parent / "vendor" / "rrweb-record.min.js").read_text(encoding="utf-8")
    return _bundle


def injected_script(rrweb: Optional[str] = None) -> str:
    """What runs in each top-level document. rrweb records the DOM and its
    changes; every 500 ms (and on page hide) the batch goes back over CDP through
    the binding, so the page itself never makes a request. Inputs are masked,
    stylesheets inlined, canvas off, and a fresh snapshot is taken every minute
    so a run that starts mid-session still has a starting point."""
    bundle = recorder_bundle() if rrweb is None else rrweb
    return (
        f'if (!globalThis.__ckReplay && window.top === window && typeof globalThis.{BINDING_NAME} === "function") {{\n'
        "globalThis.__ckReplay = { pending: true };\n"
        f"{bundle}\n"
        ";(function () {\n"
        f"  var emit = globalThis.{BINDING_NAME};\n"
        "  var api = globalThis.rrwebRecord;\n"
        '  if (!api || typeof api.record !== "function") return;\n'
        "  var record = api.record;\n"
        "  var buffer = [];\n"
        "  function flush() {\n"
        "    if (!buffer.length) return;\n"
        "    var batch = buffer;\n"
        "    buffer = [];\n"
        "    try { emit(JSON.stringify({ href: String(location.href), events: batch })); } catch (e) {}\n"
        "  }\n"
        "  var stop = record({\n"
        "    emit: function (event) { buffer.push(event); if (buffer.length >= 100) flush(); },\n"
        "    maskAllInputs: true,\n"
        "    inlineStylesheet: true,\n"
        '    slimDOMOptions: "all",\n'
        "    recordCanvas: false,\n"
        "    collectFonts: false,\n"
        "    checkoutEveryNms: 60000,\n"
        '    sampling: { mousemove: 50, scroll: 100, media: 800, input: "last" }\n'
        "  });\n"
        "  var timer = setInterval(flush, 500);\n"
        '  addEventListener("pagehide", flush, true);\n'
        "  globalThis.__ckReplay = {\n"
        "    snapshot: function () { try { record.takeFullSnapshot(true); } catch (e) {} flush(); },\n"
        "    flush: flush,\n"
        "    stop: function () { clearInterval(timer); flush(); if (stop) stop(); }\n"
        "  };\n"
        "})();\n"
        "}"
    )
