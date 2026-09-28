"""
canviz/e2e_sender.py
--------------------
Server-side periodic sender for E2E-protected frames.

The counter and CRC must change on every transmission, so these frames
cannot be driven by the browser re-POSTing fixed bytes. Each active frame
gets its own asyncio task that builds a fresh payload per send.

Jobs are keyed by a client-chosen string (the UI uses its frame id). The
counter for a key survives stop/start and single sends, so the sequence
continues instead of restarting at 0. Calling start() on a running key
updates its template / config / interval in place (used for live edits).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from canviz.bus import bus_manager
from canviz.e2e import E2EConfig, build_with_config
from canviz.stats_store import stats

log = logging.getLogger("canviz.e2e")


@dataclass
class E2EJob:
    key: str
    can_id: int
    is_extended: bool
    template: bytes
    interval_ms: int
    cfg: E2EConfig
    task: asyncio.Task | None = None
    sent: int = 0
    last_frame: bytes | None = None
    last_counter: int | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict:
        return {
            "key":          self.key,
            "id":           self.can_id,
            "running":      self.running,
            "interval_ms":  self.interval_ms,
            "sent":         self.sent,
            "last_counter": self.last_counter,
            "last_data":    list(self.last_frame) if self.last_frame is not None else None,
            "error":        self.error,
        }


class E2ESender:
    def __init__(self) -> None:
        self._jobs: dict[str, E2EJob] = {}
        self._counters: dict[str, int] = {}

    def _next_counter(self, key: str, cfg: E2EConfig) -> int:
        c = self._counters.get(key, 0) % cfg.counter_modulo
        self._counters[key] = (c + 1) % cfg.counter_modulo
        return c

    async def _transmit(self, job: E2EJob) -> bytes:
        counter = self._next_counter(job.key, job.cfg)
        frame = build_with_config(job.template, counter, job.cfg)
        await bus_manager.send(job.can_id, list(frame), job.is_extended)
        stats.on_tx(len(frame))
        job.sent += 1
        job.last_frame = frame
        job.last_counter = counter
        job.error = None
        return frame

    async def _run(self, job: E2EJob) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while True:
                try:
                    await self._transmit(job)
                except Exception as exc:
                    # Typically "not connected" — stop rather than spin
                    job.error = str(exc)
                    log.warning("E2E job %s stopped: %s", job.key, exc)
                    return
                next_at += job.interval_ms / 1000
                delay = next_at - loop.time()
                if delay < 0:
                    # Fell behind (slow adapter / paused loop): don't burst to catch up
                    next_at = loop.time()
                    delay = 0
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    def _upsert(self, key: str, can_id: int, is_extended: bool, template: bytes,
                interval_ms: int, cfg: E2EConfig) -> E2EJob:
        job = self._jobs.get(key)
        if job is None:
            job = E2EJob(key, can_id, is_extended, template, interval_ms, cfg)
            self._jobs[key] = job
        else:
            job.can_id, job.is_extended = can_id, is_extended
            job.template, job.interval_ms, job.cfg = template, interval_ms, cfg
        return job

    def start(self, key: str, can_id: int, is_extended: bool, template: bytes,
              interval_ms: int, cfg: E2EConfig) -> E2EJob:
        """Start (or update in place) a periodic E2E frame."""
        if not bus_manager.connected:
            raise RuntimeError("Not connected. Call /connect first.")
        job = self._upsert(key, can_id, is_extended, template, interval_ms, cfg)
        if not job.running:
            job.error = None
            job.task = asyncio.get_running_loop().create_task(self._run(job), name=f"e2e-{key}")
        return job

    async def send_once(self, key: str, can_id: int, is_extended: bool, template: bytes,
                        cfg: E2EConfig) -> E2EJob:
        """Send a single E2E frame, advancing the key's counter."""
        existing = self._jobs.get(key)
        interval = existing.interval_ms if existing else 0
        job = self._upsert(key, can_id, is_extended, template, interval, cfg)
        await self._transmit(job)
        return job

    async def stop(self, key: str) -> E2EJob | None:
        job = self._jobs.get(key)
        if job and job.task:
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass
            job.task = None
        return job

    async def stop_all(self) -> None:
        for key in list(self._jobs):
            await self.stop(key)

    def forget(self, key: str) -> None:
        """Drop a stopped job and its counter."""
        self._jobs.pop(key, None)
        self._counters.pop(key, None)

    def statuses(self) -> list[dict]:
        return [j.status() for j in self._jobs.values()]


# Singleton
e2e_sender = E2ESender()
