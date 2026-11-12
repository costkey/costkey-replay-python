# costkey-replay

Session replay for Python browser agents. Watch what your agent did on each page, as smooth video, lined up with the LLM call that chose each action and what the run had cost by then.

The Python counterpart of [`@costkey/replay`](https://github.com/costkey/costkey-replay). Recordings show up in the same CostKey console, whichever language recorded them.

```bash
pip install costkey-replay
```

```python
import costkey
import costkey_replay
from playwright.sync_api import sync_playwright

costkey.init()         # reads COSTKEY_DSN
costkey_replay.init()  # same DSN

with costkey.start_trace("checkout-agent"):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # ...your agent, unchanged
```

The async API works the same way. Or with no code changes:

```bash
COSTKEY_DSN=... python -m costkey_replay agent.py
```

## How it works

There's nothing to wrap. `costkey_replay` hooks the Playwright your app already uses, the same way the `costkey` SDK hooks httpx:

- **Every Chromium page gets an rrweb recording.** The recorder runs on its own CDP session, in an isolated world. The page's scripts can't see it, the page's CSP doesn't apply to it, and the page never makes a request for it. Events come back over CDP and upload in the background.
- **The agent's actions are kept**, for example `locator.click("#buy")`, `page.goto(url)` or `mouse.click(412, 230)`, with their selectors, URLs and coordinates. Typed text is never kept, only its length. rrweb masks every input field.
- **Images and fonts** come from the agent's own browser over CDP. That includes pages behind a login. Each one is uploaded once per project, so a replay never loads anything from the original site.
- **Runs:** each page is tied to the CostKey run (trace) its actions happen in, read from the `costkey` SDK's context. When the same page moves to another run, the recording starts that run from a fresh snapshot.

## Privacy

Recording is off until **both** sides opt in:

1. Your app calls `costkey_replay.init()` or runs under `python -m costkey_replay`.
2. An admin turns on **Screen recordings** for the project, under Settings, Privacy & data. While it's off, nothing is recorded or sent.

Keep pages out of recordings with URL patterns:

```python
costkey_replay.init(block_urls=["*/billing/*", "https://accounts.example.com/*"])
```

`COSTKEY_REPLAY_BLOCK_URLS` sets the same patterns, comma-separated.

Replays follow the project's body retention and are only visible to members who can read captured content.

## Safety

The recorder runs inside your agent's process, so:

- It never makes the agent wait. Copying is cheap, and uploads happen on a background thread with bounded memory.
- Any unexpected error switches recording off for the rest of the process, logs one warning and leaves the agent alone.
- If the server says recordings are off, or the DSN is wrong, it stops for good.

Pending uploads are sent when the process exits. Call `costkey_replay.flush()` to send them earlier.

## Supported

| | |
|---|---|
| Playwright for Python | 1.40 and later, sync and async API, Chromium (`launch`, `launch_persistent_context`, `connect_over_cdp`) |
| Python | 3.9 and later |

No dependencies of its own: rrweb's recorder ships inside the package and runs only in the page.

Not yet: Firefox and WebKit, agents that drive Chrome over raw CDP without Playwright, and desktop (VNC) recording.

## Options

| Option | Default | |
|---|---|---|
| `dsn` | `COSTKEY_DSN` | Your project's DSN |
| `block_urls` | `COSTKEY_REPLAY_BLOCK_URLS` | URL globs never recorded |
| `debug` | `COSTKEY_REPLAY_DEBUG=true` | Log what the recorder skips |
