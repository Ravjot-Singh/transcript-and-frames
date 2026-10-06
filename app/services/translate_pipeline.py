# Cloudinary video URL -> derived audio -> English transcript.

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass

import httpx

from ..cloudinary_delivery import build_audio_url, parse_delivery_url
from ..config import Settings
from ..errors import ChunkedUploadRejected, FeatureDisabled
from .asr import AsrProvider, AsrResult, GroqProvider
from .audio_proxy import iter_capped, open_audio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslateJob:
    cloudinary_url: str
    include_native: bool = False


@dataclass(frozen=True)
class TranslationOutcome:
    english_text: str
    detected_language: str | None
    audio_duration_sec: float | None
    transcription_confidence: float | None
    provider: str
    upload_path: str
    elapsed_sec: float
    native_text: str | None = None
    native_language: str | None = None


async def run_translation(
    job: TranslateJob,
    *,
    client: httpx.AsyncClient,
    settings: Settings,
) -> TranslationOutcome:
    started = time.perf_counter()

    if job.include_native and not settings.enable_native_transcript:
        raise FeatureDisabled(
            "include_native requires ENABLE_NATIVE_TRANSCRIPT=true on this deployment"
        )

    asset = parse_delivery_url(
        job.cloudinary_url,
        extra_hosts=settings.extra_host_list,
        allow_fetch=settings.allow_fetch_delivery,
    )
    audio_url = build_audio_url(
        asset,
        transformation=settings.audio_transformation,
        audio_format=settings.audio_format,
        api_secret=settings.cloudinary_api_secret,
        forward_query_params=settings.forward_query_param_list,
    )

    # Annotated against the protocol, not the concrete class, so a type checker
    # verifies GroqProvider still satisfies the seam another provider would use.
    provider: AsrProvider = GroqProvider(
        client,
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
        translate_model=settings.groq_model,
        native_model=settings.groq_native_model,
    )

    try:
        result = await _translate_streamed(provider, client, audio_url, settings)
        upload_path = "streamed"
    except ChunkedUploadRejected as exc:
        # Rare: the provider wants a measured body. Re-fetch (Cloudinary has the
        # derived audio cached by now) and upload from a buffer that only spills
        # to disk if it is genuinely large.
        logger.info("provider refused chunked upload, retrying via spool: %s", exc)
        result = await _translate_spooled(provider, client, audio_url, settings)
        upload_path = "spooled"

    native: AsrResult | None = None
    if job.include_native and settings.enable_native_transcript:
        native = await _transcribe_streamed(provider, client, audio_url, settings)

    return TranslationOutcome(
        english_text=result.text,
        detected_language=result.language,
        audio_duration_sec=result.duration_sec,
        transcription_confidence=result.confidence,
        provider=result.provider_label,
        upload_path=upload_path,
        elapsed_sec=round(time.perf_counter() - started, 3),
        native_text=native.text if native else None,
        native_language=native.language if native else None,
    )


# --------------------------------------------------------------------- internals


async def _translate_streamed(
    provider: AsrProvider,
    client: httpx.AsyncClient,
    audio_url: str,
    settings: Settings,
) -> AsrResult:
    async with open_audio(
        client,
        audio_url,
        max_bytes=settings.max_audio_bytes,
        max_redirects=settings.max_redirects,
        extra_hosts=settings.extra_host_list,
    ) as audio:
        body = iter_capped(
            audio.response.aiter_bytes(settings.stream_chunk_bytes),
            max_bytes=settings.max_audio_bytes,
        )
        return await provider.translate_streaming(body)


async def _transcribe_streamed(
    provider: AsrProvider,
    client: httpx.AsyncClient,
    audio_url: str,
    settings: Settings,
) -> AsrResult:
    async with open_audio(
        client,
        audio_url,
        max_bytes=settings.max_audio_bytes,
        max_redirects=settings.max_redirects,
        extra_hosts=settings.extra_host_list,
    ) as audio:
        body = iter_capped(
            audio.response.aiter_bytes(settings.stream_chunk_bytes),
            max_bytes=settings.max_audio_bytes,
        )
        return await provider.transcribe_streaming(body)


async def _translate_spooled(
    provider: AsrProvider,
    client: httpx.AsyncClient,
    audio_url: str,
    settings: Settings,
) -> AsrResult:
    with tempfile.SpooledTemporaryFile(max_size=settings.spool_threshold_bytes) as buffer:
        async with open_audio(
            client,
            audio_url,
            max_bytes=settings.max_audio_bytes,
            max_redirects=settings.max_redirects,
            extra_hosts=settings.extra_host_list,
        ) as audio:
            capped = iter_capped(
                audio.response.aiter_bytes(settings.stream_chunk_bytes),
                max_bytes=settings.max_audio_bytes,
            )
            async for chunk in capped:
                buffer.write(chunk)
        buffer.seek(0)
        return await provider.translate_file(buffer)
