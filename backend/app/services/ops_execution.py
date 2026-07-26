"""长时间运维任务使用的有界异步执行、心跳与超时原语。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


class StageTimeoutError(TimeoutError):
    """单个可观测执行阶段超过硬超时时间。"""

    def __init__(self, stage: str, timeout_seconds: float):
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f"stage '{stage}' exceeded {timeout_seconds:.0f}s hard timeout")


async def run_with_heartbeat(
    operation: Awaitable[Any],
    *,
    stage: str,
    timeout_seconds: float,
    heartbeat_seconds: float = 5.0,
    cancel_event: asyncio.Event | None = None,
    on_heartbeat: Callable[[float, float], Awaitable[None]] | None = None,
    cleanup_grace_seconds: float = 0.05,
    heartbeat_timeout_seconds: float = 1.0,
) -> Any:
    """Run one stage with a deadline and visible liveness heartbeats.

    A stage can no longer leave an operation job permanently in ``running``.
    Cancellation and timeout both cancel the child task. Cleanup and progress
    sinks are themselves bounded: a cancellation-resistant network call or a
    slow persistence callback must never defeat the stage deadline.
    """

    timeout_seconds = max(0.05, float(timeout_seconds))
    heartbeat_seconds = max(0.01, min(float(heartbeat_seconds), timeout_seconds))
    cleanup_grace_seconds = max(0.0, min(float(cleanup_grace_seconds), timeout_seconds))
    heartbeat_timeout_seconds = max(0.01, min(float(heartbeat_timeout_seconds), timeout_seconds))
    started = time.monotonic()
    task = asyncio.create_task(operation)

    def consume_result(done_task: asyncio.Task) -> None:
        with suppress(asyncio.CancelledError, Exception):
            done_task.exception()

    async def cancel_bounded(target: asyncio.Task) -> None:
        if target.done():
            consume_result(target)
            return
        target.cancel()
        done, _ = await asyncio.wait({target}, timeout=cleanup_grace_seconds)
        if target in done:
            consume_result(target)
        else:
            target.add_done_callback(consume_result)

    try:
        while True:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError
            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                raise StageTimeoutError(stage, timeout_seconds)
            done, _ = await asyncio.wait({task}, timeout=min(heartbeat_seconds, remaining))
            if task in done:
                return task.result()
            if on_heartbeat:
                heartbeat = asyncio.create_task(
                    on_heartbeat(
                        time.monotonic() - started,
                        max(0.0, timeout_seconds - (time.monotonic() - started)),
                    )
                )
                heartbeat_done, _ = await asyncio.wait(
                    {heartbeat},
                    timeout=min(heartbeat_timeout_seconds, max(0.01, remaining)),
                )
                if heartbeat in heartbeat_done:
                    # A failed progress sink must not abort the repair itself.
                    consume_result(heartbeat)
                else:
                    await cancel_bounded(heartbeat)
    finally:
        await cancel_bounded(task)
