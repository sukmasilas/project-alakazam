"""App configuration — every value comes from the environment (optionally
via a local ``.env``, loaded with python-dotenv), never hardcoded. Mirrors
Project-Noctrowl's own ``APP_LOGIN_USERNAME``/``APP_LOGIN_PASSWORD`` pattern
with Alakazam-specific env var names (CLAUDE.md's Architecture section /
this milestone's brief).
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
    login_username: str
    login_password: str
    secret_key: str


def get_settings() -> Settings:
    """Re-reads the environment each call (cheap, and lets tests override
    env vars per-test via monkeypatch without import-order surprises).
    """
    return Settings(
        database_url=_require_env("DATABASE_URL"),
        login_username=_require_env("ALAKAZAM_LOGIN_USERNAME"),
        login_password=_require_env("ALAKAZAM_LOGIN_PASSWORD"),
        secret_key=_require_env("ALAKAZAM_SECRET_KEY"),
    )
