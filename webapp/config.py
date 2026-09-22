"""App configuration — every value comes from the environment (optionally
via a local ``.env``, loaded with python-dotenv), never hardcoded.

As of 2026-09-22, this app has no login gate of its own (see webapp/app.py's
module docstring for the full security implication) — the
``ALAKAZAM_LOGIN_USERNAME``/``ALAKAZAM_LOGIN_PASSWORD``/``ALAKAZAM_SECRET_KEY``
env vars that used to be required here are gone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class MissingConfigError(RuntimeError):
    """Raised when a required env var is missing — fails loudly at startup
    rather than silently running with an insecure/undefined default.
    """


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(
            f"{name} is not set. Copy .env.example to .env and fill in real "
            f"values (see that file for what each variable is for)."
        )
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str


def get_settings() -> Settings:
    """Re-reads the environment each call (cheap, and lets tests override
    env vars per-test via monkeypatch without import-order surprises).
    """
    return Settings(
        database_url=_require_env("DATABASE_URL"),
    )
