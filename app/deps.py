# Request-scoped accessors for the objects built once in the lifespan handler.

from __future__ import annotations

from fastapi import Request

from .services.frame_pipeline import FrameJob, FrameOutcome
from .services.runner import Runner
from .services.translate_pipeline import TranslateJob, TranslationOutcome


def get_translate_runner(request: Request) -> Runner[TranslateJob, TranslationOutcome]:
    return request.app.state.translate_runner


def get_frames_runner(request: Request) -> Runner[FrameJob, FrameOutcome]:
    return request.app.state.frames_runner
