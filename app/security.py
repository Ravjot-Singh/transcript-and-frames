# Caller authentication: API key (the real gate) plus an optional CIDR allowlist.

from __future__ import annotations

import ipaddress
import secrets

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings
from .errors import AuthNotConfigured, ForbiddenOrigin, Unauthorized

API_KEY_HEADER_NAME = "x-api-key"

# auto_error=False so a missing header raises our own typed error (and therefore
# our own JSON shape) instead of FastAPI's default 403.
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def resolve_client_ip(request: Request, *, trust_proxy: bool) -> str | None:
    """Best-effort caller address, preferring X-Forwarded-For behind a trusted proxy."""
    # That header is client-controlled and trivially spoofable when the service is
    # directly exposed, so it is only consulted when TRUST_PROXY_HEADER is on.
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
    client = request.client
    return client.host if client else None


def _ip_allowed(raw_ip: str | None, settings: Settings) -> bool:
    networks = settings.allowed_networks
    if not networks:
        return True
    if not raw_ip:
        return False
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


async def require_caller(
    request: Request,
    api_key: str | None = Depends(api_key_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency guarding every non-public route."""
    if not settings.auth_configured:
        raise AuthNotConfigured("API_KEY is empty; refusing to serve protected routes")

    if not api_key or not secrets.compare_digest(api_key, settings.api_key):
        raise Unauthorized(f"missing or invalid {API_KEY_HEADER_NAME}")

    client_ip = resolve_client_ip(request, trust_proxy=settings.trust_proxy_header)
    if not _ip_allowed(client_ip, settings):
        raise ForbiddenOrigin(f"caller {client_ip!r} not in ALLOWED_IPS")
