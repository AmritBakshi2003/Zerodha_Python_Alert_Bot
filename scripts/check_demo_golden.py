#!/usr/bin/env python3
"""Check that a demo command's stdout still matches ``demo_output.txt``.

The golden file is stored with LF endings and the demo forces ``newline="\\n"``,
but Windows tooling can still hand back CRLF (and a redirect in Windows
PowerShell historically wrote UTF-16). This compares content rather than bytes
so the check fails on a changed alert, not on a newline convention.

Usage::

    python scripts/check_demo_golden.py                      # python -m kite_alerts.demo
    python scripts/check_demo_golden.py -- kite-alerts demo   # any command
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "demo_output.txt"


def normalise(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def main(argv: list[str]) -> int:
    # A leading "--" arrives when the command is passed through a shell.
    if len(argv) > 1 and argv[1] == "--":
        argv = [argv[0], *argv[2:]]
    command = argv[1:] or [sys.executable, "-m", "kite_alerts.demo"]

    if hasattr(sys.stdout, "reconfigure"):
        # The golden output contains alert emoji; a cp1252 console would raise.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # The demo already forces UTF-8, but pin it for the CLI entry point too so
    # this works on a Windows runner with a non-UTF-8 default console encoding.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    expected = normalise(GOLDEN.read_text(encoding="utf-8"))
    actual = normalise(completed.stdout)

    if completed.returncode != 0:
        print(f"FAIL: {' '.join(command)} exited {completed.returncode}", file=sys.stderr)
        if completed.stderr.strip():
            print(completed.stderr, file=sys.stderr)
        return 1

    if actual == expected:
        print(f"OK: {' '.join(command)} matches demo_output.txt ({len(expected)} lines)")
        return 0

    print(f"FAIL: {' '.join(command)} does not match demo_output.txt", file=sys.stderr)
    diff = difflib.unified_diff(
        expected, actual, fromfile="demo_output.txt", tofile="actual", lineterm=""
    )
    for line in diff:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
