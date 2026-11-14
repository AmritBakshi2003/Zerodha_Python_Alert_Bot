"""PyInstaller entry point for the experimental one-file Windows build.

Kept out of ``src/`` so the shipped package and its test coverage are untouched:
this file exists only for the optional, non-blocking ``windows-exe`` CI job.
"""

from __future__ import annotations

import sys

from kite_alerts.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
