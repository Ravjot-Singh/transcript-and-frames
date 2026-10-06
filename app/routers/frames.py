# POST /v1/frames - video metadata in, Cloudinary frame URLs out.

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..deps import get_frames_runner
from ..security import require_caller
from ..services.frame_pipeline import FrameJob, FrameOutcome
from ..services.runner import Runner

router = APIRouter(prefix="/v1", tags=["frames"], dependencies=[Depends(require_caller)])


class FramesRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cloudinary_url": (
                    "https://res.cloudinary.com/demo/video/upload/v1690000000/samples/clip.mp4"
                ),
                "video_id": "abc123",
                "duration_sec": 90.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            }
        }
    )

    cloudinary_url: str = Field(
        ..., min_length=12, description="An https Cloudinary **video** delivery URL."
    )
    video_id: str = Field(
        ...,
        min_length=1,
        description="Your identifier for this video; echoed back and used for grouping.",
    )
    duration_sec: float = Field(
        ...,
        gt=0,
        description=(
            "Video length in seconds, from the Cloudinary upload response. This is the "
            "only input to the frame-count curve, so it must be accurate."
        ),
    )
    width: int | None = Field(None, gt=0, description="Informational; not used in the math.")
    height: int | None = Field(None, gt=0, description="Informational; not used in the math.")
    has_audio: bool | None = Field(
        None, description="Informational. Frame sampling is independent of audio."
    )


class FrameItem(BaseModel):
    index: int
    timestamp_sec: float
    url: str


class FramesResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "video_id": "abc123",
                "requested_frame_count": 13,
                "frame_count": 13,
                "timestamps_sec": [18.468, 27.024, 31.959, 35.773, 39.064],
                "frames": [
                    {
                        "index": 0,
                        "timestamp_sec": 18.468,
                        "url": (
                            "https://res.cloudinary.com/demo/video/upload/"
                            "so_18.468,q_80/v1690000000/samples/clip.jpg"
                        ),
                    }
                ],
                "delivery": "url",
                "elapsed_sec": 0.0006,
            }
        }
    )

    video_id: str
    requested_frame_count: int = Field(
        ..., description="What the duration curve asked for, before de-duplication."
    )
    frame_count: int = Field(
        ...,
        description=(
            "How many frames are actually returned. Lower than requested only when "
            "two quantiles round to the same millisecond, which happens on very short videos."
        ),
    )
    timestamps_sec: list[float]
    frames: list[FrameItem]
    delivery: str = Field(
        ..., description="How frames are handed over. 'url' moves no image bytes through this service."
    )
    elapsed_sec: float


@router.post(
    "/frames",
    response_model=FramesResponse,
    summary="Compute sampling timestamps and Cloudinary frame URLs for a video",
    response_description="One URL per sampled timestamp.",
)
async def sample_frames(
    payload: FramesRequest,
    runner: Runner[FrameJob, FrameOutcome] = Depends(get_frames_runner),
) -> FramesResponse:
    """Choose how many frames the duration deserves, place them by inverse-normal quantiles, and return a Cloudinary URL that renders each one."""
    outcome = await runner.submit(
        FrameJob(
            cloudinary_url=payload.cloudinary_url,
            video_id=payload.video_id,
            duration_sec=payload.duration_sec,
            width=payload.width,
            height=payload.height,
            has_audio=payload.has_audio,
        )
    )
    return FramesResponse(
        video_id=outcome.video_id,
        requested_frame_count=outcome.requested_frame_count,
        frame_count=outcome.frame_count,
        timestamps_sec=outcome.timestamps_sec,
        frames=[
            FrameItem(index=f.index, timestamp_sec=f.timestamp_sec, url=f.url)
            for f in outcome.frames
        ],
        delivery=outcome.delivery,
        elapsed_sec=outcome.elapsed_sec,
    )
