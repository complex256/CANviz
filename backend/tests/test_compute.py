"""
tests/test_compute.py
---------------------
Scripted per-send frame values: the sandbox validator, the helper library,
and the compute pipeline. Endpoint / bus behaviour lives in test_compute_api.py.
"""

import pytest

from canviz.compute import (
    ComputeConfig,
    Counter,
    ExprError,
    Step,
    Target,
    evaluate,
    validate_expr,
)
from canviz.e2e import crc8

NAMES = {"data", "counter", "dlc", "can_id", "dataId"}


# ── Sandbox: what must be rejected ────────────────────────────────────────────

@pytest.mark.parametrize("expr", [
    "().__class__",                     # attribute access
    "().__class__.__bases__",
    "__import__('os')",                 # dunder name / import
    "open('/etc/passwd')",              # non-whitelisted call
    "eval('1')",
    "exec('x=1')",
    "getattr(data, 'x')",
    "(lambda: 1)()",                    # lambda
    "[x for x in data]",               # comprehension
    "data.pop()",                       # method call (attribute)
    "2 ** 64",                          # power operator
    "1 << 999999999",                   # huge shift literal
    "9999999999999",                    # huge integer literal
    "counter := 1",                     # walrus / assignment
    "unknownname + 1",                  # unknown name
    "__builtins__",                     # dunder name
    "f'{data}'",                        # f-string
    "{1: 2}",                           # dict literal
])
def test_validate_rejects(expr):
    with pytest.raises(ExprError):
        validate_expr(expr, NAMES)


@pytest.mark.parametrize("expr", [
    "crc8(data[1:], 0x2F, 0xFF, 0xFF)",
    "sum8(data[0:7]) & 0xFF",
    "xor8(data) ^ 0xFF",
    "counter & 0x0F",
    "twos_complement(sum8(data[1:]), 8)",
    "data[3] if counter > 0 else 0",
    "dataId[counter] + lo(data[2])",
    "mem('prev', 0) + 1",
    "crc16(data[1:] + [dataId[counter]], 0x1021, 0xFFFF, 0x0000)",
])
def test_validate_accepts(expr):
    validate_expr(expr, NAMES)  # must not raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def test_crc16_ccitt_false_check():
    from canviz.compute import crc16
    # CRC-16/CCITT-FALSE check value for "123456789" is 0x29B1
    assert crc16(b"123456789", 0x1021, 0xFFFF, 0x0000) == 0x29B1


def test_helpers_basic():
    from canviz.compute import sum8, twos_complement, xor8
    assert sum8([0x10, 0x20, 0x30]) == 0x60
    assert xor8([0xFF, 0x0F]) == 0xF0
    assert twos_complement(sum8([0x01, 0x02]), 8) == 0xFD   # checksum-complement style


# ── Pipeline ──────────────────────────────────────────────────────────────────

GRA_DATA_IDS = [0x6A, 0x38, 0xB4, 0x27, 0x22, 0xEF, 0xE1, 0xBB,
                0xF8, 0x80, 0x84, 0x49, 0xC7, 0x9E, 0x1E, 0x2B]


def _gra_pipeline() -> ComputeConfig:
    # Reproduce AUTOSAR E2E Profile 2 (GRA_ACC_01) as a compute pipeline:
    # counter in byte1 low nibble, CRC-8H2F over data[1:] + dataId[counter] at byte0.
    return ComputeConfig(
        counter=Counter(byte=1, bit_offset=0, width=4, step=1),
        steps=[Step(target=Target(byte=0), expr="crc8(data[1:] + [dataId[counter]], 0x2F, 0xFF, 0xFF)")],
        tables={"dataId": GRA_DATA_IDS},
    )


def test_pipeline_reproduces_e2e_vectors():
    """A compute pipeline must match the known-answer E2E frames byte-for-byte."""
    cfg = _gra_pipeline()
    trees = cfg.validate(8)
    expect = {0: "bd00000000000000", 1: "cf01000000000000",
              2: "6b02000000000000", 15: "360f000000000000"}
    for counter, hexstr in expect.items():
        out = evaluate(cfg, bytes(8), counter, {}, 0x12B, trees)
        assert out.hex() == hexstr


def test_pipeline_signal_bit_preserved():
    cfg = _gra_pipeline()
    template = bytearray(8)
    template[2] = 0x01                       # a signal bit
    out = evaluate(cfg, bytes(template), 3, {}, 0x12B)
    assert out.hex() == "c703010000000000"


def test_multi_step_ordering_and_bitfield():
    # Step 1 sets a nibble; step 2 checksums including step 1's write
    cfg = ComputeConfig(steps=[
        Step(target=Target(byte=1, bit_offset=4, width=4), expr="lo(counter)"),
        Step(target=Target(byte=0), expr="sum8(data[1:])"),
    ])
    trees = cfg.validate(8)
    out = evaluate(cfg, bytes(8), 3, {}, 0x100, trees)
    assert out[1] == 0x30                     # counter 3 in high nibble
    assert out[0] == 0x30                     # sum of the rest


def test_state_carry_via_store_and_mem():
    # A custom counter kept in state and echoed into byte0
    cfg = ComputeConfig(steps=[
        Step(store="n", target=Target(byte=0), expr="(mem('n', 0) + 1) & 0xFF"),
    ])
    trees = cfg.validate(8)
    state: dict = {}
    seen = [evaluate(cfg, bytes(8), 0, state, 0, trees)[0] for _ in range(3)]
    assert seen == [1, 2, 3]


def test_evaluate_surfaces_runtime_error():
    cfg = ComputeConfig(steps=[Step(target=Target(byte=0), expr="dataId[counter]")],
                        tables={"dataId": [0, 1]})
    trees = cfg.validate(8)
    with pytest.raises(ExprError):
        evaluate(cfg, bytes(8), 5, {}, 0, trees)   # index 5 out of range


def test_config_validation_errors():
    with pytest.raises(ExprError):
        ComputeConfig(steps=[]).validate(8)                       # no steps
    with pytest.raises(ExprError):
        ComputeConfig(steps=[Step(expr="1")]).validate(8)          # no target/store
    with pytest.raises(ExprError):
        ComputeConfig(steps=[Step(target=Target(byte=9), expr="1")]).validate(8)  # outside frame


def test_e2e_crc_helper_shared():
    # Sanity: the helper the pipeline uses is the audited CRC-8H2F
    assert crc8(b"123456789") == 0xDF
