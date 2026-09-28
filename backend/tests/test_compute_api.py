"""
tests/test_compute_api.py
-------------------------
Compute-frame endpoints and virtual-bus integration.
"""

import asyncio
import itertools

import can
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from canviz.bus import bus_manager
from canviz.compute_sender import compute_sender
from canviz.e2e import crc8
from canviz.server import app
from canviz.ws_broadcaster import broadcaster

GRA_ID = 0x12B
GRA_DATA_IDS = [0x6A, 0x38, 0xB4, 0x27, 0x22, 0xEF, 0xE1, 0xBB,
                0xF8, 0x80, 0x84, 0x49, 0xC7, 0x9E, 0x1E, 0x2B]

# GRA_ACC_01 as a compute pipeline (equivalent to the E2E preset)
GRA_COMPUTE = {
    "counter": {"byte": 1, "bitOffset": 0, "width": 4, "step": 1},
    "steps": [{"target": {"byte": 0}, "expr": "crc8(data[1:] + [dataId[counter]], 0x2F, 0xFF, 0xFF)"}],
    "tables": {"dataId": GRA_DATA_IDS},
}


@pytest_asyncio.fixture(autouse=True)
async def clean_state():
    yield
    await compute_sender.stop_all()
    for key in list(compute_sender._jobs):
        compute_sender.forget(key)
    bus_manager.remove_frame_callback(broadcaster.on_frame)
    await bus_manager.disconnect()
    await broadcaster.stop()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Preview (no connection / no bus) ──────────────────────────────────────────

async def test_preview_matches_e2e_vectors(client):
    r = await client.post("/compute/preview",
                          json={"id": GRA_ID, "data": [0] * 8, "compute": GRA_COMPUTE, "count": 3})
    assert r.status_code == 200
    frames = r.json()["frames"]
    assert bytes(frames[0]).hex() == "bd00000000000000"
    assert bytes(frames[1]).hex() == "cf01000000000000"
    assert bytes(frames[2]).hex() == "6b02000000000000"


async def test_preview_rejects_unsafe_expr(client):
    bad = {"steps": [{"target": {"byte": 0}, "expr": "().__class__"}]}
    r = await client.post("/compute/preview", json={"data": [0] * 8, "compute": bad})
    assert r.status_code == 422
    assert "not allowed" in r.json()["detail"]


async def test_preview_reports_bad_expression(client):
    bad = {"steps": [{"target": {"byte": 0}, "expr": "dataId[counter]"}],
           "tables": {"dataId": [1, 2]}}
    r = await client.post("/compute/preview",
                          json={"data": [0] * 8, "compute": bad, "counter": 5})
    assert r.status_code == 422   # index out of range surfaced by the dry-run


# ── Send / start ──────────────────────────────────────────────────────────────

async def test_start_requires_connection(client):
    r = await client.post("/compute/start",
                          json={"key": "c1", "id": GRA_ID, "data": [0] * 8,
                                "interval_ms": 20, "compute": GRA_COMPUTE})
    assert r.status_code == 400


async def test_send_advances_counter(client):
    await client.post("/connect", json={"interface": "virtual"})
    body = {"key": "c1", "id": GRA_ID, "data": [0] * 8, "compute": GRA_COMPUTE}
    seen = []
    for _ in range(3):
        r = await client.post("/compute/send", json=body)
        assert r.status_code == 200
        seen.append(bytes(r.json()["last_data"]))
    assert [f[1] & 0x0F for f in seen] == [0, 1, 2]
    assert seen[0].hex() == "bd00000000000000"


async def test_periodic_transmits_fresh_frames(client):
    await client.post("/connect", json={"interface": "virtual"})
    captured: list[can.Message] = []
    bus_manager.add_frame_callback(lambda m: captured.append(m))

    r = await client.post("/compute/start",
                          json={"key": "hb", "id": GRA_ID, "data": [0] * 8,
                                "interval_ms": 10, "compute": GRA_COMPUTE})
    assert r.status_code == 200 and r.json()["running"] is True
    await asyncio.sleep(0.1)
    await client.post("/compute/stop", json={"key": "hb"})

    mine = [m for m in captured if m.arbitration_id == GRA_ID]
    assert len(mine) >= 4
    counters = [m.data[1] & 0x0F for m in mine]
    for prev, cur in itertools.pairwise(counters):
        assert cur == (prev + 1) % 16
    for m in mine:
        expected = crc8(bytes(m.data[1:]) + bytes([GRA_DATA_IDS[m.data[1] & 0x0F]]))
        assert m.data[0] == expected
    assert len({m.data[0] for m in mine}) > 1


async def test_state_carry_across_sends(client):
    await client.post("/connect", json={"interface": "virtual"})
    cfg = {"steps": [{"store": "n", "target": {"byte": 0}, "expr": "(mem('n', 0) + 1) & 0xFF"}]}
    body = {"key": "c1", "id": 0x200, "data": [0] * 8, "compute": cfg}
    got = []
    for _ in range(3):
        r = await client.post("/compute/send", json=body)
        got.append(r.json()["last_data"][0])
    assert got == [1, 2, 3]
    # forget resets both counter and state
    await client.post("/compute/stop", json={"key": "c1", "forget": True})
    r = await client.post("/compute/send", json=body)
    assert r.json()["last_data"][0] == 1
