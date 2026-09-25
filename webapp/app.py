"""FastAPI app factory. See docs/design/milestone-3-web-app-design.md for
the framework choice and this module's structure.

SECURITY — NO AUTHENTICATION, NO ACCESS CONTROL (as of 2026-09-22).
=====================================================================
This app has **no login gate and no per-user permission system**. Every
page and every ``/api/*`` endpoint is reachable by anyone who can reach
this process over the network — there is no session, no credential check,
nothing. This is a deliberate architecture change, not an oversight: a new
sibling project, Dotworks (`/Users/sukmasilas/Projects/dotworks-main`), is
now the single shared login/entry point for both Project-Alakazam and its
sibling Project-Noctrowl. Once a user authenticates at Dotworks and clicks
through, they land directly here with no second login prompt — by design,
there is no session-sharing/SSO mechanism between Dotworks and this app,
just a plain trusted-network model.

**This means this process must never be reachable directly from the public
internet.** The only acceptable public entry point is Dotworks, in front of
a reverse-proxy/firewall setup that keeps this app internal-only (e.g.
bound to localhost or an internal network, with Dotworks as the sole
externally-reachable service). Do not deploy this app publicly-reachable on
its own — treat that as a real security incident waiting to happen, not a
convenience shortcut. A real per-user permission/role system remains an
explicit, deliberately deferred future milestone; until it exists, anyone
who can reach this app's URL at all has full, ungated access to every
screen and every write endpoint (purchases, depletions, consignment
reimbursements, everything).

See CLAUDE.md's "Architecture change, confirmed 2026-09-22" entry for the
full decision record.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from inventory.db import get_engine
from webapp.config import get_settings
from webapp.health import register_health_route
from webapp.pages import register_page_routes
from webapp import api as api_module

BASE_DIR = Path(__file__).resolve().parent


def _root_path_context(request: Request) -> dict:
    """Makes the reverse-proxy mount prefix (e.g. "/alakazam") available to
    every template as ``root_path``, so server-rendered links/redirects and
    the JS-facing base-path global (see base.html) come out correctly
    prefixed when this process is run with ``--root-path /alakazam`` behind
    nginx, and are an exact no-op (empty string) in local/unprefixed dev.

    ``root_path`` is ASGI's native mechanism for this (see uvicorn's
    ``--root-path`` flag) — it only affects URL *generation*, never incoming
    route matching, which is why this is safe to read directly off
    ``request.scope`` rather than needing any routing changes.
    """
    return {"root_path": request.scope.get("root_path", "") or ""}


def create_app(database_url: Optional[str] = None) -> FastAPI:
    settings = get_settings()
    resolved_db_url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        yield
        fastapi_app.state.engine.dispose()

    app = FastAPI(title="Alakazam", description="Inventory management (Milestone 3)", lifespan=lifespan)
    app.state.engine = get_engine(resolved_db_url)

    templates = Jinja2Templates(
        directory=str(BASE_DIR / "templates"),
        context_processors=[_root_path_context],
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    register_health_route(app)
    register_page_routes(app, templates)
    app.include_router(api_module.router)

    return app
