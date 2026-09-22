# Web App — Milestone 3 (Project-Alakazam)

**Superseded, 2026-09-22**: the login gate described below (`webapp/auth.py`,
`LoginRequiredMiddleware`, `ALAKAZAM_LOGIN_USERNAME`/`ALAKAZAM_LOGIN_PASSWORD`/
`ALAKAZAM_SECRET_KEY`, `/login`) has been **removed**. A new sibling project,
Dotworks, is now the single shared login/entry point for both Alakazam and
Noctrowl — see CLAUDE.md's "Architecture change, confirmed 2026-09-22" entry
and `webapp/app.py`'s module docstring for the real security implication
(Alakazam now has no authentication of its own and must never be reachable
directly from the public internet). This section is kept as-is below for
historical record of Milestone 3's original design, not as current behavior.

Real backend + UI, built against the real Milestone 2 Postgres schema and
business logic in `inventory/*.py`. This document covers the framework
choice and the module layout; the screens themselves are unchanged from
the approved `docs/design/ui-ux-design.md` / `docs/design/mockup.html` —
this milestone makes that design real, not a redesign.

## Framework choice: FastAPI

Considered: Flask (Project-Noctrowl's own choice, sibling project, same
tech stack), Django, FastAPI.

**Chosen: FastAPI**, for reasons specific to *this* project's UI, not
precedent alone:

- Every screen leans heavily on live client-side interactivity that must
  be backed by real data once wired to Postgres: typeahead SKU search,
  live SKU/serial-uniqueness checks, live cost/shipping recompute, and a
  live reconciliation check. That shape — a thin page shell plus a JSON
  API the client polls/posts to repeatedly — fits a typed JSON API layer
  better than a mostly-server-rendered-forms app (which is closer to what
  Noctrowl's own screens need: read-only reports, a review-queue table
  edited row-by-row).
- Pydantic request/response models give free validation on every
  API boundary (e.g. `PurchaseIn`, `NewItemCreateIn` in `webapp/schemas.py`)
  without hand-rolling parsing/validation for a dozen endpoints — useful
  here specifically because the Purchase Entry payload has real internal
  structure (lines, nested serial units, conditional required fields)
  that benefits from a schema, not just a few form fields.
- Async-native (`Starlette` under the hood) if a future milestone needs
  it (e.g. concurrent Drive/OCR calls in a later ingestion milestone) —
  not exercised yet in this milestone (all DB calls here are the
  synchronous `sqlalchemy` engine, matching `inventory/*.py`'s existing
  synchronous `Connection`-based API), but doesn't require a rewrite to
  add later.

Flask was not a bad choice here — it would have worked. It was not chosen
because it doesn't materially simplify anything *this* app needs relative
to FastAPI, while FastAPI's request/response typing directly matches the
Purchase Entry payload's real structure. This is a judgment call, not a
strong technical blocker either way; noting it explicitly per the brief's
instruction to justify the choice rather than default to precedent.

## Module layout

```
webapp/
  app.py         — create_app() factory: engine creation, middleware
                   (session + login-gate), template/static mounting,
                   route registration. Entry point for both the real app
                   (webapp/main.py) and the test suite (tests/webapp/).
  main.py        — ASGI entrypoint (`uvicorn webapp.main:app`).
  config.py      — Settings dataclass, reads every value from the
                   environment (never hardcoded); fails loudly if a
                   required var is missing.
  auth.py        — login/logout routes + LoginRequiredMiddleware (a
                   structural gate — a new route can never forget to
                   require login, since it's enforced for the whole ASGI
                   app, not opt-in per route).
  dbdep.py       — per-request DB connection dependencies: get_read_conn
                   (plain connection) and get_write_conn (open
                   transaction — commits on success, rolls back on any
                   exception, which is what makes save_purchase()
                   actually transactional when called from here).
  schemas.py     — Pydantic request models + straight field-mapping into
                   inventory.purchases's own dataclasses. No computation.
  api.py         — the JSON API. Every endpoint calls straight into
                   inventory/*.py; this file's own code is I/O plumbing
                   and JSON-safe serialization (Decimal -> string,
                   date -> isoformat) only.
  pages.py       — thin HTML page routes (Jinja2 shells only; real data
                   is fetched client-side from the API, same pattern
                   mockup.html already used with static arrays).
  templates/     — base.html (shared chrome) + one template per screen +
                   login.html.
  static/
    css/app.css  — ported from mockup.html's own CSS (already
                   user-reviewed/approved visually in Milestone 1).
    js/common.js — fmtIDR/escapeAttr/apiGet/apiPost helpers.
    js/calc.js   — pure client-side ports of allocation.py / sku.py /
                   serials.py, used ONLY for live preview — see its
                   module docstring for why this is safe (the server
                   never trusts it).
    js/*.js      — one file per screen (inventory, item_detail,
                   purchase_entry, purchase_history).
```

Additions to `inventory/*.py` for this milestone (all read-only or a new,
narrowly-scoped standalone creation path — no existing Milestone-2
business logic was changed):

- `inventory/queries.py` gained `list_categories`, `list_items`,
  `search_items`, `get_fungible_purchase_rows`, `get_serialized_unit_rows`,
  `list_purchases`, `get_purchase_detail` — plain SQL surfacing rows the
  schema already has, for the real screens to render. None of these
  compute money; allocation math stays exclusively in
  `inventory/allocation.py` / `inventory/purchases.py`.
- `inventory/items.py` (new module) — `create_item()`, the engine behind
  the Inventory screen's "+ Add New Item" panel. This is genuinely
  different from a purchase line's inline new-item path in
  `save_purchase()` (a purchase always needs >= 1 line, per the
  `check_purchase_has_lines` trigger; standalone item creation has none).
  Reuses the same `inventory/sku.py` generator and `inventory/uniqueness.py`
  checks `save_purchase()` itself uses — no duplicated logic.

## Login gate

Single shared username/password (`ALAKAZAM_LOGIN_USERNAME` /
`ALAKAZAM_LOGIN_PASSWORD`), compared with `hmac.compare_digest` (constant-time).
Session is a signed cookie via Starlette's `SessionMiddleware`
(`itsdangerous` under the hood), keyed on `ALAKAZAM_SECRET_KEY` — a random
generated value, not a real credential, but still env-var/`.env`-only,
never hardcoded (mirrors Project-Noctrowl's own `APP_SECRET_KEY` pattern).

Enforcement is `LoginRequiredMiddleware`, a single ASGI middleware that
gates every route except `/login`, `/health`, and `/static/*` — structural,
not a per-route decorator that a new route could forget to apply.
Unauthenticated page requests redirect to `/login`; unauthenticated API
requests get a 401 JSON body (so the client JS can redirect itself rather
than rendering a raw HTML login page inside a `fetch()` response).

No roles/permissions system — one shared login, one owner/user, matching
Noctrowl's own reasoning for the prototype stage.

## Invoice upload / OCR and per-unit photo capture — still simulated

Per the brief: no real Google Drive integration, no real OCR engine, no
real file storage in this milestone. `webapp/static/js/purchase_entry.js`
keeps the exact same filename-based OCR simulation Milestone 1's mockup
used (`invoice-toko-grosir-jaya.jpg` / `watch-receipt-handwritten.jpg`
sample buttons + a real `<input type=file>` whose filename drives the same
simulation). On Save, the invoice's filename + OCR outcome + any parsed
fields are stored on the real `purchases` row (`invoice_document_ref`,
`invoice_ocr_status`, `invoice_parsed_fields` — the Milestone 2 schema's
existing groundwork columns), so Purchase History's real, non-simulated
detail view can display them. Per-unit photos are read client-side via
`FileReader` (a real data URL, shown as a real thumbnail) but never
uploaded anywhere; only the **filename** is persisted as
`serial_units.photo_reference` (also existing groundwork). This is a
deliberate, documented choice, not an oversight: the schema field exists
specifically so a later real-ingestion milestone is additive, per
Milestone 2's own design notes.

## Money/currency on the wire

Every `Decimal` leaving the API is serialized as a **string** (e.g.
`"150000"`), never a JSON number — the same "never float" discipline
CLAUDE.md requires of the engine itself (`inventory/allocation.py`),
carried through to the wire format so the client never round-trips
currency through a JS float. Plain Python `int` fields that were never
`Decimal` to begin with (e.g. `PurchaseAllocation.running_total`, already
a real Python `int` computed by `allocation.py`) are sent as JSON numbers,
since they're already exact.

The client-side `calc.js` mirrors the server's allocation algorithm using
plain JS numbers for **live preview only** — see that file's own
docstring for why this is safe: Save always POSTs the raw draft (not any
client-computed total) to `/api/purchases`, and
`inventory.purchases.save_purchase()` on the server re-derives and
re-validates the allocation, SKU/serial generation, and uniqueness from
scratch. If the two ever disagree, the server's answer is what actually
gets saved.

## Running locally

```bash
cd project-alakazam
source .venv/bin/activate  # or create one: python3 -m venv .venv
pip install -r requirements.txt

cp .env.example .env
# fill in DATABASE_URL, ALAKAZAM_LOGIN_USERNAME/PASSWORD, ALAKAZAM_SECRET_KEY

python scripts/run_migrations.py
python -c "from inventory.db import get_engine; from inventory.seed import seed_categories; \
  eng = get_engine('<your DATABASE_URL>'); \
  conn = eng.begin().__enter__(); seed_categories(conn); conn.commit()"

uvicorn webapp.main:app --reload --port 8000
# open http://localhost:8000 — redirects to /login
```

## Running the test suite

Same disposable-Postgres pattern as Milestone 2
(`tests/_db_safety.py` / `tests/conftest.py`); the web-app suite
(`tests/webapp/`) reuses the exact same `TEST_DATABASE_URL` guard and
schema drop/recreate approach, just building a real FastAPI `TestClient`
on top instead of a bare `Connection`.

```bash
docker start alakazam-test-pg   # or create it per tests/conftest.py's docstring
export TEST_DATABASE_URL=postgresql+psycopg2://alakazam:testpass@localhost:55433/alakazam_test
pytest
```
