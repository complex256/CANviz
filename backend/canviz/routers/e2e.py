"""
canviz/routers/e2e.py
---------------------
E2E-protected frame transmission (rolling counter + CRC recomputed per send).

POST /e2e/start     — start, or update in place, a periodic E2E frame
POST /e2e/send      — send one E2E frame, advancing its counter
POST /e2e/stop      — stop a periodic E2E frame
POST /e2e/stop-all  — stop every periodic E2E frame
GET  /e2e           — status of all E2E jobs (counter, frames sent, last payload, error)

Static frames keep using POST /send unchanged.
"""

from fastapi import APIRouter, HTTPException

from canviz.bus import bus_manager
from canviz.e2e import E2EConfig
from canviz.e2e_sender import e2e_sender
from canviz.models import E2EFrameRequest, E2EStartRequest, E2EStopRequest

router = APIRouter(prefix="/e2e", tags=["e2e"])


def _prepare(req: E2EFrameRequest) -> tuple[bytes, E2EConfig]:
    dlc = len(req.data) if req.dlc is None else req.dlc
    if not 0 <= dlc <= 8:
        raise HTTPException(status_code=400, detail="CAN 2.0 data max 8 bytes.")
    if len(req.data) > 8 or any(not 0 <= b <= 0xFF for b in req.data):
        raise HTTPException(status_code=400, detail="data must be up to 8 bytes (0-255).")
    template = bytes(req.data[:dlc]).ljust(dlc, b"\x00")
    e = req.e2e
    cfg = E2EConfig(
        data_id_list=list(e.data_id_list),
        crc_index=e.crc_index,
        counter_byte=e.counter_byte,
        counter_bit_offset=e.counter_bit_offset,
        counter_width=e.counter_width,
        poly=e.crc.poly,
        init=e.crc.init,
        xor_out=e.crc.xor_out,
        profile=e.profile,
    )
    try:
        cfg.validate(dlc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return template, cfg


@router.post("/start")
async def start(req: E2EStartRequest):
    template, cfg = _prepare(req)
    try:
        job = e2e_sender.start(req.key, req.id, req.is_extended_id, template, req.interval_ms, cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job.status()


@router.post("/send")
async def send(req: E2EFrameRequest):
    if not bus_manager.connected:
        raise HTTPException(status_code=400, detail="Not connected. Call /connect first.")
    template, cfg = _prepare(req)
    try:
        job = await e2e_sender.send_once(req.key, req.id, req.is_extended_id, template, cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return job.status()


@router.post("/stop")
async def stop(req: E2EStopRequest):
    job = await e2e_sender.stop(req.key)
    if req.forget:
        e2e_sender.forget(req.key)
    return {"ok": True, "job": job.status() if job else None}


@router.post("/stop-all")
async def stop_all():
    await e2e_sender.stop_all()
    return {"ok": True}


@router.get("")
async def status():
    return {"jobs": e2e_sender.statuses()}
