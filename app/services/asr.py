# Speech provider adapter. Only Groq is implemented; AsrProvider is the seam.

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

import httpx

from ..errors import (
    AsrNotConfigured,
    AsrRateLimited,
    AsrUnavailable,
    AudioTooLarge,
    ChunkedUploadRejected,
)
from .audio_proxy import multipart_stream, new_boundary

logger = logging.getLogger(__name__)

UPLOAD_FILENAME = "audio.mp3"
UPLOAD_CONTENT_TYPE = "audio/mpeg"


@dataclass(frozen=True)
class AsrResult:
    text: str
    language: str | None
    duration_sec: float | None
    confidence: float | None
    model: str

    @property
    def provider_label(self) -> str:
        return f"groq:{self.model}"


class AsrProvider(Protocol):
    """What the pipeline needs from a speech provider."""

    async def translate_streaming(
        self, body: AsyncIterator[bytes], *, model: str | None = None
    ) -> AsrResult: ...

    async def translate_file(
        self, fileobj: BinaryIO, *, model: str | None = None
    ) -> AsrResult: ...

    async def transcribe_streaming(
        self, body: AsyncIterator[bytes], *, model: str | None = None
    ) -> AsrResult: ...


def _parse_verbose_json(payload: dict[str, Any], model: str) -> AsrResult:
    segments = payload.get("segments") or []
    logprobs = [
        seg["avg_logprob"]
        for seg in segments
        if isinstance(seg, dict) and isinstance(seg.get("avg_logprob"), (int, float))
    ]
    confidence = round(sum(logprobs) / len(logprobs), 4) if logprobs else None

    duration = payload.get("duration")
    if not isinstance(duration, (int, float)):
        duration = None

    return AsrResult(
        text=(payload.get("text") or "").strip(),
        # Passed through verbatim: providers vary between ISO codes ("hi") and
        # full names ("hindi"), and inventing a mapping would be a guess.
        language=payload.get("language") or None,
        duration_sec=float(duration) if duration is not None else None,
        confidence=confidence,
        model=model,
    )


class GroqProvider:
    """Groq's OpenAI-compatible audio endpoints."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        translate_model: str,
        native_model: str,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._translate_model = translate_model
        self._native_model = native_model

    # ----------------------------------------------------------------- public
    async def translate_streaming(
        self, body: AsyncIterator[bytes], *, model: str | None = None
    ) -> AsrResult:
        """Translate to English, streaming the audio through (no buffering)."""
        return await self._send_streaming(
            "audio/translations", body, model or self._translate_model
        )

    async def translate_file(
        self, fileobj: BinaryIO, *, model: str | None = None
    ) -> AsrResult:
        """Translate to English from a seekable file (the fallback path)."""
        return await self._send_file(
            "audio/translations", fileobj, model or self._translate_model
        )

    async def transcribe_streaming(
        self, body: AsyncIterator[bytes], *, model: str | None = None
    ) -> AsrResult:
        """Transcribe in the original language. Gated by ENABLE_NATIVE_TRANSCRIPT."""
        # A second provider call, so enabling it doubles audio-quota consumption.
        return await self._send_streaming(
            "audio/transcriptions", body, model or self._native_model
        )

    # ---------------------------------------------------------------- internal
    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise AsrNotConfigured("GROQ_API_KEY is not set")
        return {"Authorization": f"Bearer {self._api_key}"}

    def _fields(self, model: str) -> dict[str, str]:
        return {
            "model": model,
            "response_format": "verbose_json",
            # Deterministic output; this route is not a creative endpoint.
            "temperature": "0",
        }

    async def _send_streaming(
        self, path: str, body: AsyncIterator[bytes], model: str
    ) -> AsrResult:
        boundary = new_boundary()
        stream = multipart_stream(
            boundary,
            fields=self._fields(model),
            file_field="file",
            filename=UPLOAD_FILENAME,
            file_content_type=UPLOAD_CONTENT_TYPE,
            body=body,
        )
        headers = {
            **self._headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/{path}", headers=headers, content=stream
            )
        except httpx.HTTPError as exc:
            raise AsrUnavailable(f"transport error talking to Groq: {exc!r}") from exc
        return self._handle(response, model, streaming=True)

    async def _send_file(self, path: str, fileobj: BinaryIO, model: str) -> AsrResult:
        try:
            response = await self._client.post(
                f"{self._base_url}/{path}",
                headers=self._headers(),
                data=self._fields(model),
                files={"file": (UPLOAD_FILENAME, fileobj, UPLOAD_CONTENT_TYPE)},
            )
        except httpx.HTTPError as exc:
            raise AsrUnavailable(f"transport error talking to Groq: {exc!r}") from exc
        return self._handle(response, model, streaming=False)

    def _handle(self, response: httpx.Response, model: str, *, streaming: bool) -> AsrResult:
        status = response.status_code

        if status == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise AsrUnavailable("Groq returned a non-JSON success body") from exc
            if not isinstance(payload, dict):
                raise AsrUnavailable("Groq returned an unexpected JSON shape")
            return _parse_verbose_json(payload, model)

        body = response.text[:500]

        if streaming and _looks_like_length_required(status, body):
            # Signals the pipeline to retry once with a measured body.
            raise ChunkedUploadRejected(body)

        if status in (401, 403):
            raise AsrUnavailable(f"Groq rejected the credentials ({status})")
        if status == 413:
            raise AudioTooLarge("Groq rejected the audio as too large")
        if status == 429:
            retry_after = _retry_after_seconds(response)
            raise AsrRateLimited(
                f"Groq rate limit / quota exhausted: {body}", retry_after=retry_after
            )
        if status >= 500:
            raise AsrUnavailable(f"Groq returned {status}: {body}")
        raise AsrUnavailable(f"Groq returned {status}: {body}")


def _looks_like_length_required(status: int, body: str) -> bool:
    """Whether the provider refused the request for lacking a Content-Length."""
    if status == 411:
        return True
    if status == 400:
        lowered = body.lower()
        return "length" in lowered or "transfer-encoding" in lowered
    return False


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return None
