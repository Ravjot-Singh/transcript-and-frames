# Unauthenticated service metadata. Reachable by the keep-alive cron, reveals no secret.

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import __version__
from ..config import Settings, get_settings

router = APIRouter(tags=["meta"])


@router.get("/", summary="Service banner")
async def root(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "online",
        "service": settings.service_name,
        "version": __version__,
        "features": ["translate", "frames"],
        "docs": "/docs" if settings.enable_docs else None,
        "demo": "/demo" if settings.enable_demo else None,
    }


@router.get("/health", summary="Liveness and configuration check")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    """Liveness and configuration. Makes no upstream call, so it is safe to poll."""
    return {
        "status": "healthy",
        "auth_configured": settings.auth_configured,
        "asr_configured": settings.asr_configured,
        "cloudinary_signing": settings.cloudinary_signing_enabled,
        "ip_allowlist_active": bool(settings.allowed_networks),
        "native_transcript_enabled": settings.enable_native_transcript,
    }
