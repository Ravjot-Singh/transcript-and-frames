# GET /demo - a static test console, served same-origin so no CORS setup is needed.

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..config import Settings, get_settings
from ..errors import AppError

router = APIRouter(tags=["demo"], include_in_schema=False)

DEMO_PAGE = Path(__file__).resolve().parent.parent / "static" / "demo.html"


class DemoDisabled(AppError):
    status_code = 404
    code = "demo_disabled"
    public_message = "The test console is not enabled on this deployment."


@router.get("/demo", response_class=HTMLResponse, summary="Interactive test console")
async def demo(settings: Settings = Depends(get_settings)) -> HTMLResponse:
    if not settings.enable_demo:
        raise DemoDisabled("ENABLE_DEMO is false")
    try:
        html = DEMO_PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(f"could not read {DEMO_PAGE.name}: {exc!r}") from exc
    # no-store so edits to the page show up on reload during development.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
