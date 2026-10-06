# Application factory and process-wide wiring.

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, get_settings
from .errors import AppError
from .routers import demo as demo_router
from .routers import frames as frames_router
from .routers import meta as meta_router
from .routers import translate as translate_router
from .services.frame_pipeline import FrameJob, run_frames
from .services.runner import InlineRunner
from .services.translate_pipeline import TranslateJob, run_translation

logger = logging.getLogger("app")


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings)

    timeout = httpx.Timeout(
        connect=settings.http_connect_timeout,
        read=settings.http_read_timeout,
        write=settings.http_write_timeout,
        pool=settings.http_pool_timeout,
    )
    # Each in-flight translation holds two connections at once (Cloudinary and the
    # provider), so the pool is sized above that to leave headroom.
    limits = httpx.Limits(
        max_connections=settings.max_concurrent_asr * 2 + 6,
        max_keepalive_connections=8,
    )
    client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)

    asr_semaphore = asyncio.Semaphore(settings.max_concurrent_asr)

    # Both handlers read the client and settings off app.state rather than
    # closing over them, so a test (or a future reconfiguration) can swap either
    # one without rebuilding the runners.
    async def translate_handler(job: TranslateJob):
        return await run_translation(
            job, client=app.state.http_client, settings=app.state.settings
        )

    async def frames_handler(job: FrameJob):
        return await run_frames(job, settings=app.state.settings)

    app.state.settings = settings
    app.state.http_client = client
    app.state.translate_runner = InlineRunner(translate_handler, semaphore=asr_semaphore)
    # Frame sampling is pure arithmetic, so it needs no concurrency limit of its own.
    app.state.frames_runner = InlineRunner(frames_handler)

    if not settings.auth_configured:
        logger.warning(
            "API_KEY is not set: /v1 routes will answer 503 until it is configured"
        )
    if not settings.asr_configured:
        logger.warning("GROQ_API_KEY is not set: /v1/translate will fail at call time")

    logger.info(
        "%s v%s ready (asr_concurrency=%s, docs=%s)",
        settings.service_name,
        __version__,
        settings.max_concurrent_asr,
        settings.enable_docs,
    )
    try:
        yield
    finally:
        await client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Transcript and Frames API",
        version=__version__,
        description=(
            "Two independent capabilities over Cloudinary-hosted video:\n\n"
            "* **POST /v1/translate** — speech in any Whisper-supported language to "
            "English text.\n"
            "* **POST /v1/frames** — duration-aware frame sampling, returned as "
            "Cloudinary image URLs.\n\n"
            "Both require the `x-api-key` header (use **Authorize** above). "
            "No media is stored by this service."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    if settings.allowed_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "x-api-key"],
        )

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # The detail can contain signed URLs and provider messages, so it is
        # logged against a correlation id and only echoed when DEBUG_ERRORS is on.
        correlation_id = uuid.uuid4().hex[:12]
        logger.warning(
            "%s %s -> %s %s [%s] %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            correlation_id,
            exc.detail,
        )
        body: dict[str, object] = {
            "success": False,
            "error": exc.code,
            "message": exc.public_message,
            "correlation_id": correlation_id,
        }
        if get_settings().debug_errors:
            body["detail"] = exc.detail

        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = uuid.uuid4().hex[:12]
        logger.exception(
            "unhandled error on %s %s [%s]", request.method, request.url.path, correlation_id
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "internal_error",
                "message": "An unexpected error occurred.",
                "correlation_id": correlation_id,
            },
        )

    app.include_router(meta_router.router)
    app.include_router(demo_router.router)
    app.include_router(translate_router.router)
    app.include_router(frames_router.router)
    return app


app = create_app()
