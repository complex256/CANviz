"""
canviz/routers/compute.py
-------------------------
Scripted "compute" frames: a per-send pipeline (optional counter + sandboxed
expression steps) that derives frame bytes on every transmission.

POST /compute/start     — start / update a periodic compute frame
POST /compute/send      — send one compute frame (advances the counter)
POST /compute/stop      — stop a periodic compute frame
POST /compute/stop-all  — stop every periodic compute frame
POST /compute/preview   — evaluate without sending (for the editor)
GET  /compute           — status of all compute jobs

The expression sandbox is documented in canviz/compute.py.
"""

from fastapi import APIRouter, HTTPException

from canviz.bus import bus_manager
from canviz.compute import ComputeConfig, Counter, ExprError, Step, Target, evaluate
from canviz.compute_sender import compute_sender
from canviz.models import (
    ComputeConfigModel,
    ComputeFrameRequest,
    ComputePreviewRequest,
    ComputeStartRequest,
    ComputeStopRequest,
)

router = APIRouter(prefix="/compute", tags=["compute"])


def _to_config(model: ComputeConfigModel) -> ComputeConfig:
    return ComputeConfig(
        version=model.version,
        counter=Counter(model.counter.byte, model.counter.bit_offset,
                        model.counter.width, model.counter.step) if model.counter else None,
        steps=[
            Step(
                expr=s.expr,
                target=Target(s.target.byte, s.target.bit_offset, s.target.width) if s.target else None,
                store=s.store,
            )
            for s in model.steps
        ],
        tables={k: list(v) for k, v in model.tables.items()},
    )


def _prepare(req: ComputeFrameRequest):
    dlc = len(req.data) if req.dlc is None else req.dlc
    if not 0 < dlc <= 8:
        raise HTTPException(status_code=400, detail="CAN 2.0 data is 1..8 bytes.")
    if len(req.data) > 8 or any(not 0 <= b <= 0xFF for b in req.data):
        raise HTTPException(status_code=400, detail="data must be up to 8 bytes (0-255).")
    template = bytes(req.data[:dlc]).ljust(dlc, b"\x00")
    cfg = _to_config(req.compute)
    try:
        trees = cfg.validate(dlc)              # sandbox + layout validation
        evaluate(cfg, template, 0, {}, req.id, trees)  # dry-run: surface runtime errors now
    except ExprError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return template, cfg, trees


@router.post("/start")
async def start(req: ComputeStartRequest):
    template, cfg, trees = _prepare(req)
    try:
        job = compute_sender.start(req.key, req.id, req.is_extended_id, template,
                                   req.interval_ms, cfg, trees)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job.status()


@router.post("/send")
async def send(req: ComputeFrameRequest):
    if not bus_manager.connected:
        raise HTTPException(status_code=400, detail="Not connected. Call /connect first.")
    template, cfg, trees = _prepare(req)
    try:
        job = await compute_sender.send_once(req.key, req.id, req.is_extended_id, template, cfg, trees)
    except ExprError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return job.status()


@router.post("/preview")
async def preview(req: ComputePreviewRequest):
    """Evaluate the pipeline without touching the bus (for the editor)."""
    dlc = len(req.data) if req.dlc is None else req.dlc
    if not 0 < dlc <= 8:
        raise HTTPException(status_code=400, detail="CAN 2.0 data is 1..8 bytes.")
    template = bytes(req.data[:dlc]).ljust(dlc, b"\x00")
    cfg = _to_config(req.compute)
    try:
        trees = cfg.validate(dlc)
        state: dict = {}
        frames = [list(evaluate(cfg, template, req.counter + i, state, req.id, trees))
                  for i in range(req.count)]
    except ExprError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"frames": frames}


@router.post("/stop")
async def stop(req: ComputeStopRequest):
    job = await compute_sender.stop(req.key)
    if req.forget:
        compute_sender.forget(req.key)
    return {"ok": True, "job": job.status() if job else None}


@router.post("/stop-all")
async def stop_all():
    await compute_sender.stop_all()
    return {"ok": True}


@router.get("")
async def status():
    return {"jobs": compute_sender.statuses()}
