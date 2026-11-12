"""A tiny browser agent for the tests: python tests/browser_agent.py sync|async.

It runs in its own process so each test starts from an unpatched Playwright.
The run (trace) comes from a ContextVar standing in for the core SDK's context.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import costkey_replay  # noqa: E402
from costkey_replay import _trace  # noqa: E402

run: contextvars.ContextVar = contextvars.ContextVar("run", default={})
_trace.set_getter(lambda: run.get())
costkey_replay.init(debug=True)

SHOP = os.environ["SHOP"]
LAUNCH = {"headless": True, **({"channel": os.environ["BROWSER_CHANNEL"]} if os.environ.get("BROWSER_CHANNEL") else {})}


def sync_agent() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**LAUNCH)
        token = run.set({"traceId": "trace_first"})
        page = browser.new_page()
        page.goto(f"{SHOP}/shop")
        page.click("#add")
        page.fill("#email", "agent@example.com")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        run.reset(token)
        run.set({"traceId": "trace_second"})
        page.click("#next")
        page.wait_for_load_state()
        page.locator("#pay").click()
        page.wait_for_timeout(1500)
        browser.close()


async def async_agent() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**LAUNCH)
        token = run.set({"traceId": "trace_first"})
        page = await browser.new_page()
        await page.goto(f"{SHOP}/shop")
        await page.click("#add")
        await page.fill("#email", "agent@example.com")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)
        run.reset(token)
        run.set({"traceId": "trace_second"})
        await page.click("#next")
        await page.wait_for_load_state()
        await page.locator("#pay").click()
        await page.wait_for_timeout(1500)
        await browser.close()


if __name__ == "__main__":
    if sys.argv[1] == "sync":
        sync_agent()
    else:
        asyncio.run(async_agent())
    print("flushed", costkey_replay.flush(10))
