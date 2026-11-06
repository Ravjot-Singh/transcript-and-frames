# Video metadata -> frame timestamps -> Cloudinary frame URLs. No network calls.

from __future__ import annotations

import time
from dataclasses import dataclass

from ..cloudinary_delivery import build_frame_url, parse_delivery_url
from ..config import Settings
from ..errors import DurationTooLong, InvalidDuration
from .frame_math import compute_frame_count, compute_frame_timestamps
from .frame_sink import Frame, get_sink


@dataclass(frozen=True)
class FrameJob:
    cloudinary_url: str
    video_id: str
    duration_sec: float
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None
    max_frames: int | None = None


@dataclass(frozen=True)
class FrameOutcome:
    video_id: str
    requested_frame_count: int
    frame_count: int
    timestamps_sec: list[float]
    frames: list[Frame]
    frame_urls: list[str]
    delivery: str
    elapsed_sec: float


async def run_frames(job: FrameJob, *, settings: Settings) -> FrameOutcome:
    started = time.perf_counter()

    if job.duration_sec is None or job.duration_sec <= 0:
        raise InvalidDuration(f"duration_sec must be > 0, got {job.duration_sec!r}")

    max_sec = settings.max_allowed_duration_min * 60.0
    if settings.reject_over_max_duration and job.duration_sec > max_sec:
        raise DurationTooLong(
            f"duration_sec {job.duration_sec} exceeds the configured maximum {max_sec}"
        )

    asset = parse_delivery_url(
        job.cloudinary_url,
        extra_hosts=settings.extra_host_list,
        allow_fetch=settings.allow_fetch_delivery,
    )

    duration_min = job.duration_sec / 60.0
    requested = compute_frame_count(
        duration_min,
        a=settings.frame_a,
        f_min=settings.frame_fmin,
        f_max=settings.frame_fmax,
        max_allowed_duration_min=settings.max_allowed_duration_min,
        sigma_l=settings.sigma_l,
        sigma_r=settings.sigma_r,
    )
    # A cap narrows how many frames are produced, but never the curve: the response
    # keeps reporting what the curve asked for. The quantile function is re-run with
    # the smaller N rather than thinning the full list, because N points drawn from
    # the same inverse-normal distribution stay properly spread and still dense at
    # the midpoint - dropping every k-th element of a longer list would flatten that.
    cap = job.max_frames if job.max_frames is not None else settings.max_frames_default
    effective = min(requested, cap) if cap else requested

    timestamps = compute_frame_timestamps(
        job.duration_sec,
        effective,
        sigma_divisor=settings.timestamp_sigma_divisor,
    )

    frames = [
        Frame(
            index=index,
            timestamp_sec=timestamp,
            url=build_frame_url(
                asset,
                timestamp_sec=timestamp,
                quality=settings.frame_jpeg_quality,
                width=settings.frame_width,
                api_secret=settings.cloudinary_api_secret,
                forward_query_params=settings.forward_query_param_list,
            ),
        )
        for index, timestamp in enumerate(timestamps)
    ]

    sink = get_sink(settings.frame_sink)
    delivery_meta = await sink.deliver(job.video_id, frames)

    return FrameOutcome(
        video_id=job.video_id,
        # requested is what the curve asked for; frame_count is what survived both the
        # optional cap and millisecond de-duplication, which can differ on short videos.
        requested_frame_count=requested,
        frame_count=len(frames),
        timestamps_sec=timestamps,
        frames=frames,
        # The same URLs as a flat array, so a caller that wants only the list - such as
        # an image-moderation service taking {"urls": [...]} - needs no mapping step.
        frame_urls=[frame.url for frame in frames],
        delivery=str(delivery_meta.get("delivery", sink.name)),
        elapsed_sec=round(time.perf_counter() - started, 4),
    )
