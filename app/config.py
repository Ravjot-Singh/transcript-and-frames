# All configuration, as environment variables. See .env.example for meanings.

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Which dotenv file to load, resolved relative to the working directory. The test
# suite points this at a path that does not exist so that it never inherits the
# developer's real .env - otherwise any variable the tests do not explicitly
# override would leak in, and the suite would pass locally but fail in CI.
# A missing file is ignored silently.
ENV_FILE = os.environ.get("CONFIG_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- service
    service_name: str = "transcript-and-frames"
    api_key: str = ""
    allowed_ips: str = ""
    trust_proxy_header: bool = True
    allowed_origins: str = ""
    enable_docs: bool = True
    enable_demo: bool = False
    log_level: str = "info"
    debug_errors: bool = False

    # ------------------------------------------------------------- cloudinary
    cloudinary_api_secret: str = ""
    cloudinary_extra_hosts: str = ""
    allow_fetch_delivery: bool = False
    # Query parameters carried from the incoming URL onto the derived one.
    # "*" forwards everything; empty drops the query entirely.
    cloudinary_forward_query_params: str = "__cld_token__"

    # -------------------------------------------------------------------- asr
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # whisper-large-v3-turbo is transcription-only and CANNOT translate.
    groq_model: str = "whisper-large-v3"
    groq_native_model: str = "whisper-large-v3-turbo"
    enable_native_transcript: bool = False

    # ------------------------------------------------------------------ audio
    audio_transformation: str = "af_16000,br_32k"
    audio_format: str = "mp3"
    max_audio_bytes: int = 20 * 1024 * 1024
    spool_threshold_bytes: int = 8 * 1024 * 1024
    stream_chunk_bytes: int = 64 * 1024
    max_redirects: int = 3

    # ------------------------------------------------------------ concurrency
    max_concurrent_asr: int = 3

    # --------------------------------------------------------------- timeouts
    http_connect_timeout: float = 5.0
    http_read_timeout: float = 120.0
    http_write_timeout: float = 120.0
    http_pool_timeout: float = 5.0

    # ----------------------------------------------------------------- frames
    frame_a: float = 60.0
    frame_fmin: int = 8
    frame_fmax: int = 60
    max_allowed_duration_min: float = 10.0
    sigma_l: float = 2.0
    sigma_r: float = 4.0
    timestamp_sigma_divisor: float = 6.0

    # Cloudinary q_ value (1-100, HIGHER is better) — not the ffmpeg -q:v scale.
    frame_jpeg_quality: int = 80
    frame_width: int = 0
    # Only "url" is implemented; the other two are documented stubs that raise
    # NotImplementedError when a request reaches them. Typos are rejected at
    # startup rather than per request.
    frame_sink: Literal["url", "bytes", "local"] = "url"
    reject_over_max_duration: bool = False

    # ------------------------------------------------------------- properties
    @property
    def allowed_origin_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def extra_host_list(self) -> tuple[str, ...]:
        return tuple(
            h.strip().lower() for h in self.cloudinary_extra_hosts.split(",") if h.strip()
        )

    @property
    def forward_query_param_list(self) -> tuple[str, ...]:
        return tuple(
            p.strip()
            for p in self.cloudinary_forward_query_params.split(",")
            if p.strip()
        )

    @property
    def allowed_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """Parsed ALLOWED_IPS; an empty tuple means IP filtering is disabled."""
        nets = []
        for raw in self.allowed_ips.split(","):
            raw = raw.strip()
            if not raw:
                continue
            nets.append(ipaddress.ip_network(raw, strict=False))
        return tuple(nets)

    @property
    def asr_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def auth_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def cloudinary_signing_enabled(self) -> bool:
        return bool(self.cloudinary_api_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
