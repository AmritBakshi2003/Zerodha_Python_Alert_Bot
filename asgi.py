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

from kite_alerts.ui.app import build_context, create_app

app = create_app(build_context())

__all__ = ["app"]
