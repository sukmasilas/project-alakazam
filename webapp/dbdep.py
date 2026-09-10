"""Per-request DB connection dependencies, on top of the single Engine
created once at app startup (see webapp/app.py's lifespan). Two flavors,
matching how inventory/*.py's own functions expect to be called:

- ``get_read_conn`` — a plain connection for read-only queries (GET
  endpoints). No transaction semantics needed.
- ``get_write_conn`` — a connection with an open transaction
  (``engine.begin()``), committed on a normal return and rolled back if the
  endpoint raises. This is what makes ``inventory.purchases.save_purchase()``
  (which deliberately never commits/rolls back itself — see its module
  docstring) actually transactional when called from the web app: the
  caller here is this dependency, exactly as that module's docstring
  expects.
"""
from __future__ import annotations

from typing import Generator

from fastapi import Request
from sqlalchemy.engine import Connection, Engine


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_read_conn(request: Request) -> Generator[Connection, None, None]:
    engine = get_engine(request)
    with engine.connect() as conn:
        yield conn


def get_write_conn(request: Request) -> Generator[Connection, None, None]:
    engine = get_engine(request)
    with engine.begin() as conn:
        yield conn
