# The seam where a job queue can later replace inline execution.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

JobT = TypeVar("JobT")
ResultT = TypeVar("ResultT")


class Runner(Protocol[JobT, ResultT]):
    async def submit(self, job: JobT) -> ResultT: ...


class InlineRunner(Generic[JobT, ResultT]):
    """Runs the handler in the caller's request, bounded by a semaphore."""
    # The semaphore is what stops a burst of callers exceeding the provider's rate
    # limit, while leaving the event loop free for everything that is not an ASR call.

    def __init__(
        self,
        handler: Callable[[JobT], Awaitable[ResultT]],
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._handler = handler
        self._semaphore = semaphore

    async def submit(self, job: JobT) -> ResultT:
        if self._semaphore is None:
            return await self._handler(job)
        async with self._semaphore:
            return await self._handler(job)
