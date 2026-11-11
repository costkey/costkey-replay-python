"""Real Chromium, driven by Playwright, against a stand-in CostKey server.

Each agent runs in its own process, so every test starts from an unpatched
Playwright. Skipped when Playwright or a Chromium build isn't available.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fake_server import FakeCostKey

pytest.importorskip("playwright")

AGENT = Path(__file__).with_name("browser_agent.py")


def run_agent(mode: str, fake: FakeCostKey) -> subprocess.CompletedProcess:
    env = {**os.environ, "COSTKEY_DSN": fake.dsn, "SHOP": fake.base}
    result = subprocess.run([sys.executable, str(AGENT), mode], env=env, capture_output=True, text=True, timeout=180)
    if result.returncode != 0 and ("Executable doesn't exist" in result.stderr or "is not found at" in result.stderr):
        pytest.skip("no Chromium for Playwright here")
    assert result.returncode == 0, result.stderr[-3000:]
    return result


@pytest.fixture
def fake():
    server = FakeCostKey().start()
    yield server
    server.stop()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_records_the_pages_an_agent_drives(mode, fake):
    result = run_agent(mode, fake)
    seen = f"runs={[chunk['traceId'] for chunk in fake.chunks]} actions={fake.custom('costkey.action')} log={result.stderr[-1500:]}"
    assert fake.chunks, "no chunks arrived"
    assert len({chunk["recordingId"] for chunk in fake.chunks}) == 1
    types = {event.get("type") for event in fake.events()}
    assert {2, 3, 4, 5} <= types, seen  # Full snapshot, changes, meta, custom.
    # Each run's part of the recording is tagged with that run.
    assert {chunk["traceId"] for chunk in fake.chunks} == {"trace_first", "trace_second"}, seen
    actions = fake.custom("costkey.action")
    for expected in (
        {"name": "page.click", "selector": "#add"},
        {"name": "page.fill", "selector": "#email", "textLength": 17},
        {"name": "keyboard.keyboardPress", "key": "Enter"},
        {"name": "locator.click", "selector": "#pay"},
    ):
        assert expected in actions, seen
    # The image was uploaded once and referenced from the recording.
    assert len(fake.assets) == 1 and fake.custom("costkey.asset"), seen
    # What the agent typed never left the process.
    assert "agent@example.com" not in json.dumps(fake.chunks)
    assert fake.headers[0]["User-Agent"].startswith("costkey-replay-python/")


def test_records_nothing_when_the_project_has_recordings_off():
    fake = FakeCostKey(screen_capture=False).start()
    try:
        run_agent("sync", fake)
        assert fake.chunks == [] and fake.assets == {}
    finally:
        fake.stop()
