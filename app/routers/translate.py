# POST /v1/translate - Cloudinary video URL in, English transcript out.

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..deps import get_translate_runner
from ..security import require_caller
from ..services.runner import Runner
from ..services.translate_pipeline import TranslateJob, TranslationOutcome

router = APIRouter(prefix="/v1", tags=["translate"], dependencies=[Depends(require_caller)])


class TranslateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cloudinary_url": (
                    "https://res.cloudinary.com/demo/video/upload/v1690000000/samples/clip.mp4"
                )
            }
        }
    )

    cloudinary_url: str = Field(
        ...,
        min_length=12,
        description=(
            "An https Cloudinary **video** delivery URL. Transformations, folders, "
            "versions and missing extensions are all handled. Non-Cloudinary hosts "
            "are rejected."
        ),
    )
    include_native: bool = Field(
        default=False,
        description=(
            "Also return the transcript in the original spoken language. Requires "
            "ENABLE_NATIVE_TRANSCRIPT=true; otherwise this returns 400 feature_disabled. "
            "It costs a second provider call, doubling audio-quota use."
        ),
    )


class TranslateResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "detected_language": "hi",
                "english_text": (
                    "At the Taj Mahal in Agra, the 300th Urs of Mughal emperor Shah Jahan "
                    "was inaugurated."
                ),
                "audio_duration_sec": 41.2,
                "transcription_confidence": -0.21,
                "confidence_scale": "mean_avg_logprob",
                "provider": "groq:whisper-large-v3",
                "upload_path": "streamed",
                "elapsed_sec": 4.118,
            }
        }
    )

    success: bool = True
    detected_language: str | None = Field(
        None,
        description=(
            "Language the provider detected in the audio, passed through verbatim. "
            "Providers differ between ISO codes ('hi') and names ('hindi')."
        ),
    )
    english_text: str = Field(..., description="The transcript, translated to English.")
    audio_duration_sec: float | None = Field(
        None, description="Audio length as reported by the provider."
    )
    transcription_confidence: float | None = Field(
        None,
        description=(
            "Mean per-segment avg_logprob. This is transcription confidence, NOT "
            "language-identification confidence — hosted Whisper APIs do not expose "
            "the latter. Closer to 0 is better; typical range -0.1 to -0.5."
        ),
    )
    confidence_scale: str = "mean_avg_logprob"
    provider: str
    upload_path: str = Field(
        ..., description="How the audio reached the provider: 'streamed' or 'spooled'."
    )
    elapsed_sec: float
    native_text: str | None = None
    native_language: str | None = None


@router.post(
    "/translate",
    response_model=TranslateResponse,
    summary="Translate the speech in a Cloudinary video to English",
    response_description="The English transcript plus the detected source language.",
)
async def translate_video(
    payload: TranslateRequest,
    runner: Runner[TranslateJob, TranslationOutcome] = Depends(get_translate_runner),
) -> TranslateResponse:
    """Derive an audio-only Cloudinary URL, stream that audio to the speech provider, and return the English transcript. The transcript's content is not inspected."""
    outcome = await runner.submit(
        TranslateJob(
            cloudinary_url=payload.cloudinary_url,
            include_native=payload.include_native,
        )
    )
    return TranslateResponse(
        detected_language=outcome.detected_language,
        english_text=outcome.english_text,
        audio_duration_sec=outcome.audio_duration_sec,
        transcription_confidence=outcome.transcription_confidence,
        provider=outcome.provider,
        upload_path=outcome.upload_path,
        elapsed_sec=outcome.elapsed_sec,
        native_text=outcome.native_text,
        native_language=outcome.native_language,
    )
