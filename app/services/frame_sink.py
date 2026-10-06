# Where extracted frames are handed over. FrameSink is the remote-analyzer seam.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Frame:
    index: int
    timestamp_sec: float
    url: str


class FrameSink(Protocol):
    """Delivers the computed frames to wherever they need to go."""

    name: str

    async def deliver(self, video_id: str, frames: list[Frame]) -> dict[str, Any]: ...


class UrlListSink:
    """Default: hand back Cloudinary URLs and move no image bytes at all."""

    name = "url"

    async def deliver(self, video_id: str, frames: list[Frame]) -> dict[str, Any]:
        return {"delivery": self.name}


class BytesForwardSink:
    """Stub: fetch each frame and forward the bytes to the analysis service."""
    # Needs the remote contract first: endpoint, field name, batching and auth.
    # Also costs a download plus an upload per frame, 60 of each for a long video.

    name = "bytes"

    async def deliver(self, video_id: str, frames: list[Frame]) -> dict[str, Any]:
        raise NotImplementedError(
            "BytesForwardSink needs the remote frame-analysis contract "
            "(endpoint, schema, auth) before it can be implemented"
        )


class LocalFileSink:
    """Stub: write frames to ``storage/frames/<video_id>/`` as the old pipeline did."""
    # Unsuitable here: the free instance has no persistent disk, and keeping local
    # copies of user media is something this service deliberately avoids.

    name = "local"

    async def deliver(self, video_id: str, frames: list[Frame]) -> dict[str, Any]:
        raise NotImplementedError(
            "LocalFileSink is intentionally unwired; the default URL sink needs no storage"
        )


def get_sink(name: str) -> FrameSink:
    sinks: dict[str, FrameSink] = {
        UrlListSink.name: UrlListSink(),
        BytesForwardSink.name: BytesForwardSink(),
        LocalFileSink.name: LocalFileSink(),
    }
    try:
        return sinks[name]
    except KeyError:
        raise ValueError(f"unknown FRAME_SINK {name!r}; valid: {sorted(sinks)}") from None
