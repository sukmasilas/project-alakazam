"""ASGI entrypoint for running the real app.

    uvicorn webapp.main:app --reload --port 8000

Reads all configuration from the environment (see webapp/config.py and
.env.example) — never hardcodes a connection string or credential.
"""
from __future__ import annotations

from webapp.app import create_app

app = create_app()
