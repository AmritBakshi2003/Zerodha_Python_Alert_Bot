"""ASGI entrypoint for Vercel's Python builder.

Vercel looks for a module-level ``app`` object in one of a handful of root-level
files (see ``tool.vercel.entrypoint`` in its docs). This module supplies that
object by wiring the same production context and app factory the local CLI uses.

The dashboard's DNS-rebinding guard stays on: ``ui.security.SecurityMiddleware``
still refuses an unexpected ``Host`` header. A hosted deployment is trusted
because ``vercel.json`` ships the static assets and Vercel exposes the host names
it serves this build from (``VERCEL_URL`` and friends), which the middleware
accepts. A local run is untouched and remains loopback-only.

The deployment is a read-only demo shell: there is no ``.env``, no alert
database and no bot process in the bundle, so it cannot place orders, read
credentials or send notifications.
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
