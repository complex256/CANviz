"""
tests/test_e2e.py
-----------------
E2E (AUTOSAR Profile 2, VW/Audi flavour) frame protection.

Unit tests pin the CRC and frame layout to known-answer vectors; integration
tests capture frames from the virtual bus to check the counter advances and
the CRC is recomputed on every transmission.

Run: pytest tests/ -v
"""

import asyncio
import itertools

import can
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from canviz.bus import bus_manager
from canviz.e2e import E2EConfig, build_e2e_frame, build_with_config, crc8, crc8h2f
from canviz.e2e_sender import e2e_sender
from canviz.server import app
from canviz.ws_broadcaster import broadcaster

# GRA_ACC_01 (0x12B) Data-ID list
GRA_DATA_IDS = [0x6A, 0x38, 0xB4, 0x27, 0x22, 0xEF, 0xE1, 0xBB,
                0xF8, 0x80, 0x84, 0x49, 0xC7, 0x9E, 0x1E, 0x2B]
GRA_ID = 0x12B

VW_P2 = {
    "profile": "vw_p2", "crcIndex": 0, "counterByte": 1, "counterBitOffset": 0,
    "counterWidth": 4, "crc": {"poly": 47, "init": 255, "xorOut": 255},
    "dataIdList": GRA_DATA_IDS,
}


# ── CRC algorithm ───────────────────────────────────────────────────────────

def test_crc8h2f_check_value():
    # CRC-8/AUTOSAR check value
    assert crc8h2f(b"123456789") == 0xDF


def test_crc8h2f_range8():
    assert crc8h2f(bytes(range(8))) == 0xFC


def test_crc8_matches_crc8h2f_with_defaults():
    for sample in (b"123456789", bytes(range(8)), b"\x00", bytes([0xDE, 0xAD, 0xBE, 0xEF])):
        assert crc8(sample) == crc8h2f(sample)


# ── build_e2e_frame known-answer vectors (GRA_ACC_01) ─────────────────────────

def _neutral(counter: int) -> bytes:
    return build_e2e_frame(bytes(8), counter, GRA_DATA_IDS)

def test_kat_neutral_counters():
    assert _neutral(0).hex()  == "bd00000000000000"
    assert _neutral(1).hex()  == "cf01000000000000"
    assert _neutral(2).hex()  == "6b02000000000000"
    assert _neutral(15).hex() == "360f000000000000"

def test_kat_signal_bit_set():
    # bit16 set == byte2 bit0; counter 3 in byte1 low nibble
    template = bytearray(8)
    template[2] = 0x01
    assert build_e2e_frame(bytes(template), 3, GRA_DATA_IDS).hex() == "c703010000000000"


# ── Layout behaviour ──────────────────────────────────────────────────────────

def test_counter_written_into_configured_field():
    cfg = E2EConfig(data_id_list=GRA_DATA_IDS)
    for counter in range(16):
        frame = build_with_config(bytes(8), counter, cfg)
        assert frame[1] & 0x0F == counter          # counter lands in byte1 low nibble
        assert frame[0] == crc8h2f(bytes(frame[1:]) + bytes([GRA_DATA_IDS[counter]]))

def test_counter_wraps_modulo_width():
    cfg = E2EConfig(data_id_list=GRA_DATA_IDS)
    # counter 16 masks to 0 -> identical to counter 0
    assert build_with_config(bytes(8), 16, cfg) == build_with_config(bytes(8), 0, cfg)

def test_data_id_index_tracks_counter():
    ids = list(range(16))  # data_id == counter, easy to see the fold-in
    cfg = E2EConfig(data_id_list=ids)
    for counter in range(16):
        frame = build_with_config(bytes(8), counter, cfg)
        assert frame[0] == crc8h2f(bytes(frame[1:]) + bytes([counter]))

def test_signal_bits_preserved():
    template = bytes([0x00, 0xF0, 0xAB, 0xCD, 0, 0, 0, 0])  # byte1 high nibble is signal
    frame = build_e2e_frame(template, 5, GRA_DATA_IDS)
    assert frame[1] == 0xF5                 # high nibble kept, low nibble = counter
    assert frame[2:] == template[2:]        # bytes after CRC untouched apart from counter byte

def test_config_validation():
    with pytest.raises(ValueError):
        E2EConfig(data_id_list=[0] * 15).validate(8)          # wrong Data-ID length
    with pytest.raises(ValueError):
        E2EConfig(data_id_list=[0] * 16, crc_index=0, counter_byte=0).validate(8)  # overlap
    with pytest.raises(ValueError):
        E2EConfig(data_id_list=[0] * 16, crc_index=8).validate(8)  # outside frame
    with pytest.raises(ValueError):
        E2EConfig(data_id_list=[0] * 16, counter_bit_offset=6, counter_width=4).validate(8)


# ── Fixtures for HTTP / bus integration ───────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def clean_state():
    yield
    await e2e_sender.stop_all()
    for key in list(e2e_sender._jobs):
        e2e_sender.forget(key)
    bus_manager.remove_frame_callback(broadcaster.on_frame)
    await bus_manager.disconnect()
    await broadcaster.stop()

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Endpoint behaviour ────────────────────────────────────────────────────────

async def test_e2e_start_requires_connection(client):
    body = {"key": "f1", "id": GRA_ID, "data": [0] * 8, "interval_ms": 20, "e2e": VW_P2}
    r = await client.post("/e2e/start", json=body)
    assert r.status_code == 400

async def test_e2e_send_once_advances_counter(client):
    await client.post("/connect", json={"interface": "virtual"})
    body = {"key": "f1", "id": GRA_ID, "data": [0] * 8, "e2e": VW_P2}
    seen = []
    for _ in range(3):
        r = await client.post("/e2e/send", json=body)
        assert r.status_code == 200
        seen.append(bytes(r.json()["last_data"]))
    assert [f[1] & 0x0F for f in seen] == [0, 1, 2]
    assert seen[0].hex() == "bd00000000000000"
    assert seen[1].hex() == "cf01000000000000"

async def test_e2e_validation_error(client):
    await client.post("/connect", json={"interface": "virtual"})
    bad = {"key": "f1", "id": GRA_ID, "data": [0] * 8,
           "e2e": {**VW_P2, "dataIdList": [0] * 15}}
    r = await client.post("/e2e/send", json=bad)
    assert r.status_code == 422


# ── Integration: capture real transmitted frames off the virtual bus ──────────

async def test_e2e_periodic_transmits_fresh_frames(client):
    """
    Start a periodic E2E frame and capture what actually lands on the bus.
    Assert the counter increments and the CRC is recomputed every frame.
    """
    await client.post("/connect", json={"interface": "virtual"})

    captured: list[can.Message] = []
    bus_manager.add_frame_callback(lambda m: captured.append(m))

    body = {"key": "hb", "id": GRA_ID, "data": [0] * 8, "interval_ms": 10, "e2e": VW_P2}
    r = await client.post("/e2e/start", json=body)
    assert r.status_code == 200 and r.json()["running"] is True

    await asyncio.sleep(0.1)          # ~10 frames at 10 ms
    await client.post("/e2e/stop", json={"key": "hb"})

    mine = [m for m in captured if m.arbitration_id == GRA_ID]
    assert len(mine) >= 4, f"expected several frames, got {len(mine)}"

    counters = [m.data[1] & 0x0F for m in mine]
    # Counter advances by 1 (mod 16) each transmission
    for prev, cur in itertools.pairwise(counters):
        assert cur == (prev + 1) % 16
    # Every frame carries the correct recomputed CRC (not a frozen byte)
    for m in mine:
        expected = crc8h2f(bytes(m.data[1:]) + bytes([GRA_DATA_IDS[m.data[1] & 0x0F]]))
        assert m.data[0] == expected
    assert len({m.data[0] for m in mine}) > 1  # CRC actually changes

async def test_e2e_counter_continues_across_start_stop(client):
    await client.post("/connect", json={"interface": "virtual"})
    body = {"key": "f1", "id": GRA_ID, "data": [0] * 8, "e2e": VW_P2}
    (await client.post("/e2e/send", json=body))          # counter 0
    (await client.post("/e2e/send", json=body))          # counter 1
    r = await client.post("/e2e/send", json=body)        # counter 2
    assert r.json()["last_counter"] == 2
    # forget resets the sequence
    await client.post("/e2e/stop", json={"key": "f1", "forget": True})
    r = await client.post("/e2e/send", json=body)
    assert r.json()["last_counter"] == 0
