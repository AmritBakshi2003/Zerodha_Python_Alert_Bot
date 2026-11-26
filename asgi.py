"""ASGI entrypoint for Vercel's Python builder.

Vercel looks for a module-level ``app`` object in one of a handful of root-level
files (see ``tool.vercel.entrypoint`` in its docs). This module supplies that
object by wiring the same production context and app factory the local CLI uses.

The dashboard remains loopback-only on purpose: ``ui.security.SecurityMiddleware``
refuses any non-loopback ``Host`` header, so a public deployment of this app
answers HTTP 400. The real, usable dashboard still runs locally via
``kite-alerts ui``. This entrypoint exists so the repository builds on Vercel
without weakening that local-only security model.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Vercel's Python runtime puts the project root on sys.path but does not
# necessarily install this package (it uses a src/ layout). Make the entrypoint
# self-contained so `import kite_alerts` works on Vercel as well as locally.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kite_alerts.ui.app import build_context, create_app  # noqa: E402

app = create_app(build_context())

__all__ = ["app"]
