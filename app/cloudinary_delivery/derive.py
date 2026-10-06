# Build derived audio (.mp3) and frame (.jpg) delivery URLs from a parsed asset.

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence

from ..errors import UnsupportedCloudinaryUrl
from .parse import DEFAULT_FORWARD_QUERY_PARAMS, CloudinaryAsset, filter_query


def _sign(transformations: Sequence[str], public_id_with_extension: str, api_secret: str) -> str:
    """Cloudinary's ``s--xxxxxxxx--`` signed-delivery component."""
    # Covers the transformations joined with the public id, so adding a
    # transformation means the signature must be recomputed.
    payload = "/".join([*transformations, public_id_with_extension])
    digest = hashlib.sha1(f"{payload}{api_secret}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode()[:8]
    return f"s--{token}--"


def _assemble(
    asset: CloudinaryAsset,
    *,
    transformations: Sequence[str],
    extension: str,
    api_secret: str,
    forward_query_params: tuple[str, ...],
) -> str:
    public_id_with_extension = (
        f"{asset.public_id}.{extension}" if extension else asset.public_id
    )

    signature: str | None = None
    if asset.signature or api_secret:
        if not api_secret:
            # Keeping the original signature would produce a URL Cloudinary
            # rejects, and silently dropping it would 401 on a strict account.
            raise UnsupportedCloudinaryUrl(
                "this URL is signed, so deriving audio/frames from it requires "
                "CLOUDINARY_API_SECRET to re-sign the result"
            )
        signature = _sign(transformations, public_id_with_extension, api_secret)

    components = [
        asset.cloud_name,
        asset.resource_type,
        asset.delivery_type,
        signature or "",
        *transformations,
        asset.version or "",
    ]
    prefix = "/".join(c for c in components if c)
    # The public id is appended rather than joined so a remote (fetch) URL keeps
    # its own "https://" intact.
    url = f"{asset.origin}/{prefix}/{public_id_with_extension}"

    query = filter_query(asset.query, forward_query_params)
    if query:
        url = f"{url}?{query}"
    return url


def _format_components(
    asset: CloudinaryAsset, output_format: str
) -> tuple[list[str], str]:
    """Extension swap for stored assets, ``f_<format>`` for remote (fetch) sources."""
    # A fetch source's extension belongs to the origin server, so it is left alone.
    if asset.is_remote:
        return [f"f_{output_format}"], ""
    return [], output_format


def build_audio_url(
    asset: CloudinaryAsset,
    *,
    transformation: str = "af_16000,br_32k",
    audio_format: str = "mp3",
    api_secret: str = "",
    forward_query_params: tuple[str, ...] = DEFAULT_FORWARD_QUERY_PARAMS,
) -> str:
    """Audio-only delivery URL for ``asset``."""
    # 16 kHz / 32 kbps costs no accuracy because Whisper downsamples to 16 kHz mono
    # anyway, and it cuts a ten-minute video from ~10 MB to ~2.4 MB in transit.
    # Cloudinary exposes no channel/mono parameter, so none is requested.
    format_transforms, extension = _format_components(asset, audio_format)
    extra = [t for t in (transformation.strip(),) if t]
    return _assemble(
        asset,
        transformations=[*asset.transformations, *extra, *format_transforms],
        extension=extension,
        api_secret=api_secret,
        forward_query_params=forward_query_params,
    )


def build_frame_url(
    asset: CloudinaryAsset,
    *,
    timestamp_sec: float,
    quality: int = 80,
    width: int = 0,
    api_secret: str = "",
    forward_query_params: tuple[str, ...] = DEFAULT_FORWARD_QUERY_PARAMS,
) -> str:
    """Single-frame JPEG delivery URL for ``asset`` at ``timestamp_sec``."""
    # quality is a Cloudinary q_ value (1-100, higher is better), NOT the inverted
    # ffmpeg -q:v scale the original local extractor used.
    parts = [f"so_{timestamp_sec:.3f}"]
    if quality:
        parts.append(f"q_{quality}")
    if width:
        # c_limit never upscales, so a width larger than the source is harmless.
        parts.extend([f"w_{width}", "c_limit"])

    format_transforms, extension = _format_components(asset, "jpg")
    return _assemble(
        asset,
        transformations=[*asset.transformations, ",".join(parts), *format_transforms],
        extension=extension,
        api_secret=api_secret,
        forward_query_params=forward_query_params,
    )
