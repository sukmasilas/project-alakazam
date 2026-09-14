"""HTML page routes — thin shells only. Every page fetches its real data
from the JSON API (webapp/api.py) client-side, mirroring
docs/design/mockup.html's own client-driven rendering approach, just now
backed by real Postgres data instead of static in-page arrays.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates


def register_page_routes(app, templates: Jinja2Templates) -> None:
    @app.get("/")
    async def index():
        return RedirectResponse(url="/inventory", status_code=303)

    @app.get("/inventory")
    async def inventory_page(request: Request):
        return templates.TemplateResponse(request, "inventory.html", {"active_nav": "inventory"})

    @app.get("/items/{sku}")
    async def item_detail_page(request: Request, sku: str):
        return templates.TemplateResponse(
            request, "item_detail.html", {"active_nav": "inventory", "sku": sku}
        )

    @app.get("/purchases/new")
    async def purchase_entry_page(request: Request):
        return templates.TemplateResponse(
            request, "purchase_entry.html", {"active_nav": "purchase"}
        )

    @app.get("/purchases")
    async def purchase_history_page(request: Request, ref: str = ""):
        return templates.TemplateResponse(
            request, "purchase_history.html", {"active_nav": "history", "auto_open_ref": ref}
        )

    @app.get("/sales")
    async def sales_log_page(request: Request, sku: str = ""):
        return templates.TemplateResponse(
            request, "sales_log.html", {"active_nav": "sales", "filter_sku": sku}
        )

    @app.get("/ebay-import")
    async def ebay_import_page(request: Request):
        return templates.TemplateResponse(
            request, "ebay_import.html", {"active_nav": "ebay_import"}
        )

    @app.get("/preorders")
    async def preorder_sales_page(request: Request):
        return templates.TemplateResponse(
            request, "preorder_sales.html", {"active_nav": "preorders"}
        )
