"""Bare liveness endpoint. Deliberately unauthenticated (matches the rest
of the app since the 2026-09-22 login-gate removal — see webapp/app.py's
module docstring) and deliberately minimal: no database round-trip, just
confirms the process is up and serving requests. This is the standard
target for a reverse proxy / uptime monitor to poll, which matters more
now that Alakazam is expected to sit behind one (see the trusted-network-
only deployment requirement in webapp/app.py).

Kept as its own tiny module rather than folded into webapp/pages.py, since
pages.py is specifically for the HTML page shells (its own docstring:
"thin shells only... every page fetches its real data from the JSON API")
— this isn't a page, and doesn't render a template.
"""
from __future__ import annotations


def register_health_route(app) -> None:
    @app.get("/health")
    async def health():
        return {"status": "ok"}
