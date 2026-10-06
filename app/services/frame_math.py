# The two frame-allocation formulas. Pure, no I/O, no third-party dependencies.

from __future__ import annotations

import math
from statistics import NormalDist

_STANDARD_NORMAL = NormalDist()


def compute_frame_count(
    duration_min: float,
    *,
    a: float = 60.0,
    f_min: int = 8,
    f_max: int = 60,
    max_allowed_duration_min: float = 10.0,
    sigma_l: float = 2.0,
    sigma_r: float = 4.0,
) -> int:
    """Number of frames to sample from a video of ``duration_min`` minutes."""
    #   mu    = max_allowed_duration_min / 2
    #   sigma = sigma_l if d <= mu else sigma_r
    #   F(d)  = clamp(round(a * exp(-(d - mu)^2 / (2 * sigma^2))), f_min, f_max)
    # Peaks at the midpoint (60 frames at 5 min by default) and is asymmetric: with
    # sigma_l < sigma_r the count falls away faster below the midpoint than above it.
    # The exponent is always negative, so extreme durations underflow to 0.0 safely.
    mu = max_allowed_duration_min / 2.0
    sigma = sigma_l if duration_min <= mu else sigma_r
    if sigma <= 0:
        raise ValueError("sigma must be positive")

    raw_frames = a * math.exp(-((duration_min - mu) ** 2) / (2.0 * sigma**2))
    return min(f_max, max(f_min, round(raw_frames)))


def compute_frame_timestamps(
    duration_sec: float,
    n_frames: int,
    *,
    sigma_divisor: float = 6.0,
) -> list[float]:
    """Timestamps for ``n_frames`` samples across a ``duration_sec`` video."""
    #   p_i = (i + 0.5) / N          midpoint of each equal-probability band
    #   t_i = T/2 + (T/divisor) * PPF(p_i)
    # Clamped to [0, T], rounded to milliseconds, then de-duplicated in order - so the
    # result can be SHORTER than n_frames when quantiles collide on very short videos.
    # The inverse CDF is monotonic, so the output is sorted and denser near the middle.
    # A single requested frame lands exactly at the midpoint.
    if n_frames <= 0:
        return []

    mu_time = duration_sec / 2.0
    sigma_time = duration_sec / sigma_divisor if sigma_divisor else 0.0

    timestamps: list[float] = []
    seen: set[float] = set()
    for i in range(n_frames):
        probability = (i + 0.5) / n_frames
        raw = mu_time + sigma_time * _STANDARD_NORMAL.inv_cdf(probability)
        bounded = min(duration_sec, max(0.0, raw))
        rounded = round(bounded, 3)
        if rounded not in seen:
            seen.add(rounded)
            timestamps.append(rounded)
    return timestamps
