"""Login gate — a single shared username/password (CLAUDE.md's brief: "no
roles/permissions system needed, one owner/user"). Session is a signed
cookie (Starlette's ``SessionMiddleware``, itself built on ``itsdangerous``),
keyed off ``ALAKAZAM_SECRET_KEY`` — never a real credential itself, but
still env-var-only / gitignored, same discipline as the login
username/password (see webapp/config.py).

Enforcement is a single ASGI middleware (``LoginRequiredMiddleware``) rather
than a per-route dependency, so a new page/API route can never be added
later and accidentally forget to require login — the gate is structural,
not opt-in per route.
"""
from __future__ import annotations

import hmac

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.templating import Jinja2Templates

from webapp.config import get_settings

# Paths reachable with no session at all.
_PUBLIC_PATHS = {"/login", "/health"}
_PUBLIC_PREFIXES = ("/static/",)


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


class LoginRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path) or request.session.get("logged_in"):
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated."}, status_code=401)

        next_qs = f"?next={request.url.path}" if request.url.path != "/" else ""
        return RedirectResponse(url=f"/login{next_qs}", status_code=303)


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def register_auth_routes(app, templates: Jinja2Templates) -> None:
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/inventory"):  # noqa: A002
        if request.session.get("logged_in"):
            return RedirectResponse(url=next or "/inventory", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "next": next}
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/inventory"),  # noqa: A002
    ):
        settings = get_settings()
        ok = _constant_time_eq(username, settings.login_username) and _constant_time_eq(
            password, settings.login_password
        )
        if not ok:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Incorrect username or password.", "next": next},
                status_code=401,
            )
        request.session["logged_in"] = True
        return RedirectResponse(url=next or "/inventory", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/health")
    async def health():
        return {"status": "ok"}
