# Stream audio from Cloudinary to the ASR provider without ever buffering it whole.

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from ..cloudinary_delivery.parse import assert_host_allowed
from ..errors import AudioTooLarge, UpstreamMediaError


@dataclass(frozen=True)
class OpenAudio:
    """A live, unread audio response body."""

    response: httpx.Response
    final_url: str
    content_type: str
    content_length: int | None


@asynccontextmanager
async def open_audio(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    extra_hosts: tuple[str, ...] = (),
) -> AsyncIterator[OpenAudio]:
    """Open ``url`` for streaming, re-validating every redirect hop."""
    current = url
    response: httpx.Response | None = None

    for _ in range(max_redirects + 1):
        request = client.build_request("GET", current, headers={"Accept": "*/*"})
        candidate = await client.send(request, stream=True, follow_redirects=False)

        if candidate.is_redirect:
            location = candidate.headers.get("location", "")
            await candidate.aclose()
            if not location:
                raise UpstreamMediaError("redirect response without a Location header")
            current = urljoin(current, location)
            assert_host_allowed(current, extra_hosts)
            continue

        response = candidate
        break

    if response is None:
        raise UpstreamMediaError(f"exceeded {max_redirects} redirects fetching audio")

    try:
        if response.status_code >= 400:
            raise UpstreamMediaError(
                f"Cloudinary returned {response.status_code} for the derived audio URL"
            )

        declared: int | None = None
        raw_length = response.headers.get("content-length")
        if raw_length:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = None
        if declared is not None and declared > max_bytes:
            raise AudioTooLarge(f"declared content-length {declared} exceeds cap {max_bytes}")

        yield OpenAudio(
            response=response,
            final_url=current,
            content_type=response.headers.get("content-type", "audio/mpeg"),
            content_length=declared,
        )
    finally:
        await response.aclose()


async def iter_capped(
    source: AsyncIterator[bytes],
    *,
    max_bytes: int,
) -> AsyncIterator[bytes]:
    """Pass bytes through, aborting if more than ``max_bytes`` arrive."""
    total = 0
    async for chunk in source:
        total += len(chunk)
        if total > max_bytes:
            raise AudioTooLarge(f"audio stream exceeded cap {max_bytes} bytes while reading")
        yield chunk


# --------------------------------------------------------------------- multipart


def new_boundary() -> str:
    return f"----transcriptframes{secrets.token_hex(16)}"


def multipart_prologue(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_content_type: str,
) -> bytes:
    """Everything in the multipart body before the file's bytes."""
    lines: list[str] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{name}"')
        lines.append("")
        lines.append(value)
    lines.append(f"--{boundary}")
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'
    )
    lines.append(f"Content-Type: {file_content_type}")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode()


def multipart_epilogue(boundary: str) -> bytes:
    return f"\r\n--{boundary}--\r\n".encode()


async def multipart_stream(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_content_type: str,
    body: AsyncIterator[bytes],
) -> AsyncIterator[bytes]:
    """A complete multipart/form-data body with ``body`` spliced in as a file."""
    yield multipart_prologue(
        boundary,
        fields=fields,
        file_field=file_field,
        filename=filename,
        file_content_type=file_content_type,
    )
    async for chunk in body:
        yield chunk
    yield multipart_epilogue(boundary)
