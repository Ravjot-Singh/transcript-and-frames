# Typed domain errors: HTTP status + stable code + a message safe to return.

from __future__ import annotations


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    public_message: str = "An unexpected error occurred."

    def __init__(self, detail: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(detail or self.public_message)
        self.detail = detail or self.public_message
        self.retry_after = retry_after


# ------------------------------------------------------------------ access
class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    public_message = "Missing or invalid x-api-key header."


class ForbiddenOrigin(AppError):
    status_code = 403
    code = "forbidden_origin"
    public_message = "Caller address is not allowed."


class AuthNotConfigured(AppError):
    status_code = 503
    code = "auth_not_configured"
    public_message = "This deployment has no API_KEY set and refuses to serve protected routes."


# ------------------------------------------------------------------ request
class InvalidCloudinaryUrl(AppError):
    status_code = 400
    code = "invalid_cloudinary_url"
    public_message = "The supplied URL is not a valid Cloudinary video delivery URL."


class UnsupportedCloudinaryUrl(AppError):
    status_code = 400
    code = "unsupported_cloudinary_url"
    public_message = "This Cloudinary URL shape is not supported by this service."


class InvalidDuration(AppError):
    status_code = 400
    code = "invalid_duration"
    public_message = "duration_sec must be greater than 0."


class DurationTooLong(AppError):
    status_code = 400
    code = "duration_too_long"
    public_message = "The video exceeds the maximum allowed duration."


class FeatureDisabled(AppError):
    status_code = 400
    code = "feature_disabled"
    public_message = "That option is not enabled on this deployment."


# ----------------------------------------------------------------- upstream
class AudioTooLarge(AppError):
    status_code = 413
    code = "audio_too_large"
    public_message = "The derived audio exceeds the maximum allowed size."


class UpstreamMediaError(AppError):
    status_code = 502
    code = "upstream_media_error"
    public_message = "Cloudinary could not deliver the requested media."


class AsrNotConfigured(AppError):
    status_code = 503
    code = "asr_not_configured"
    public_message = "Speech recognition is not configured on this deployment."


class AsrRateLimited(AppError):
    status_code = 503
    code = "asr_rate_limited"
    public_message = "Speech recognition quota exhausted. Retry later."


class AsrUnavailable(AppError):
    status_code = 502
    code = "asr_unavailable"
    public_message = "The speech recognition provider is unavailable."


# ----------------------------------------------------------------- internal
class ChunkedUploadRejected(Exception):
    """Internal signal: the provider refused a chunked multipart body."""
    # Never reaches a client; it triggers the one-time spool fallback.
