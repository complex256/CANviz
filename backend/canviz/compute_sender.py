"""
canviz/compute_sender.py
------------------------
Server-side periodic sender for scripted "compute" frames.

Mirrors e2e_sender: one asyncio task per active frame, the counter and the
per-frame `state` dict survive stop/start/single-send, and start() updates a
running job in place. The pipeline (canviz.compute) is evaluated fresh on every
transmission.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from canviz.bus import bus_manager
from canviz.compute import ComputeConfig, evaluate
from canviz.stats_store import stats

log = logging.getLogger("canviz.compute")


@dataclass
class ComputeJob:
    key: str
    can_id: int
    is_extended: bool
    template: bytes
    interval_ms: int
    cfg: ComputeConfig
    trees: list = field(default_factory=list)      # pre-validated ASTs
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


class ComputeSender:
    def __init__(self) -> None:
        self._jobs: dict[str, ComputeJob] = {}
        self._counters: dict[str, int] = {}
        self._state: dict[str, dict] = {}

    def _next_counter(self, key: str, cfg: ComputeConfig) -> int:
        c = self._counters.get(key, 0)
        step = cfg.counter.step if cfg.counter else 1
        self._counters[key] = c + step
        return c

    async def _transmit(self, job: ComputeJob) -> bytes:
        counter = self._next_counter(job.key, job.cfg)
        state = self._state.setdefault(job.key, {})
        frame = evaluate(job.cfg, job.template, counter, state, job.can_id, job.trees)
        await bus_manager.send(job.can_id, list(frame), job.is_extended)
        stats.on_tx(len(frame))
        job.sent += 1
        job.last_frame = frame
        job.last_counter = counter
        job.error = None
        return frame

    async def _run(self, job: ComputeJob) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while True:
                try:
                    await self._transmit(job)
                except Exception as exc:
                    job.error = str(exc)
                    log.warning("compute job %s stopped: %s", job.key, exc)
                    return
                next_at += job.interval_ms / 1000
                delay = next_at - loop.time()
                if delay < 0:
                    next_at = loop.time()
                    delay = 0
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    def _upsert(self, key: str, can_id: int, is_extended: bool, template: bytes,
                interval_ms: int, cfg: ComputeConfig, trees: list) -> ComputeJob:
        job = self._jobs.get(key)
        if job is None:
            job = ComputeJob(key, can_id, is_extended, template, interval_ms, cfg, trees)
            self._jobs[key] = job
        else:
            job.can_id, job.is_extended = can_id, is_extended
            job.template, job.interval_ms, job.cfg, job.trees = template, interval_ms, cfg, trees
        return job

    def start(self, key: str, can_id: int, is_extended: bool, template: bytes,
              interval_ms: int, cfg: ComputeConfig, trees: list) -> ComputeJob:
        if not bus_manager.connected:
            raise RuntimeError("Not connected. Call /connect first.")
        job = self._upsert(key, can_id, is_extended, template, interval_ms, cfg, trees)
        if not job.running:
            job.error = None
            job.task = asyncio.get_running_loop().create_task(self._run(job), name=f"compute-{key}")
        return job

    async def send_once(self, key: str, can_id: int, is_extended: bool, template: bytes,
                        cfg: ComputeConfig, trees: list) -> ComputeJob:
        existing = self._jobs.get(key)
        interval = existing.interval_ms if existing else 0
        job = self._upsert(key, can_id, is_extended, template, interval, cfg, trees)
        await self._transmit(job)
        return job

    async def stop(self, key: str) -> ComputeJob | None:
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
        self._jobs.pop(key, None)
        self._counters.pop(key, None)
        self._state.pop(key, None)

    def statuses(self) -> list[dict]:
        return [j.status() for j in self._jobs.values()]


# Singleton
compute_sender = ComputeSender()
