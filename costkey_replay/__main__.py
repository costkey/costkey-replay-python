"""``python -m costkey_replay agent.py [args]`` (or ``-m package.module``):
record without code changes. Reads ``COSTKEY_DSN``, and optionally
``COSTKEY_REPLAY_BLOCK_URLS`` (comma-separated URL globs) and
``COSTKEY_REPLAY_DEBUG=true``."""
from __future__ import annotations

import os
import runpy
import sys

USAGE = "usage: python -m costkey_replay <script.py> [args...]\n       python -m costkey_replay -m <module> [args...]"


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if args else 2)
    import costkey_replay

    costkey_replay.init()
    if args[0] == "-m":
        if len(args) < 2:
            print(USAGE, file=sys.stderr)
            sys.exit(2)
        sys.argv = [args[1], *args[2:]]
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        return
    path = args[0]
    sys.argv = list(args)
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    runpy.run_path(path, run_name="__main__")


if __name__ == "__main__":
    main()
