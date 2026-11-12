import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def python(code: str, *args: str, env: dict = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args] if args else [sys.executable, "-c", code],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
    )


def test_init_without_a_dsn_does_nothing():
    result = python("import costkey_replay; costkey_replay.init(); print(costkey_replay._state is None)", env={"COSTKEY_DSN": ""})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_init_works_without_the_core_sdk_or_a_reachable_server():
    code = (
        "import sys; sys.modules['costkey'] = None\n"  # As if the core SDK weren't installed.
        "import costkey_replay\n"
        "costkey_replay.init('http://ck_test@127.0.0.1:9/proj_test')\n"
        "print(costkey_replay.flush(1))\n"
    )
    result = python(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_the_runner_starts_recording_then_runs_the_script(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import sys, costkey_replay\nprint(costkey_replay._state is not None, sys.argv[1:])\n")
    result = python("", "-m", "costkey_replay", str(script), "--fast", env={"COSTKEY_DSN": "http://ck_test@127.0.0.1:9/proj_test"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True ['--fast']"
