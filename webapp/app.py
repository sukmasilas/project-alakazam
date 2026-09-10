"""FastAPI app factory. See docs/design/milestone-3-web-app-design.md for
the framework choice and this module's structure.

Middleware order matters here: Starlette wraps middleware such that the
*last* one added via ``add_middleware`` runs *first* (outermost) on the way
in. ``SessionMiddleware`` must run before ``LoginRequiredMiddleware`` (the
latter reads ``request.session``), so ``LoginRequiredMiddleware`` is added
first and ``SessionMiddleware`` last — verified by
tests/webapp/test_auth.py, not just asserted here.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from inventory.db import get_engine
from webapp.auth import LoginRequiredMiddleware, register_auth_routes
from webapp.config import get_settings
from webapp.pages import register_page_routes
from webapp import api as api_module

BASE_DIR = Path(__file__).resolve().parent


def create_app(database_url: Optional[str] = None, secret_key: Optional[str] = None) -> FastAPI:
    settings = get_settings()
    resolved_db_url = database_url or settings.database_url
    resolved_secret_key = secret_key or settings.secret_key

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        yield
        fastapi_app.state.engine.dispose()

    app = FastAPI(title="Alakazam", description="Inventory management (Milestone 3)", lifespan=lifespan)
    app.state.engine = get_engine(resolved_db_url)

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # See module docstring for why this order is required.
    app.add_middleware(LoginRequiredMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=resolved_secret_key,
        session_cookie="alakazam_session",
        same_site="lax",
    )

    register_auth_routes(app, templates)
    register_page_routes(app, templates)
    app.include_router(api_module.router)

    return app
