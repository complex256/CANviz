"""
canviz/e2e.py
-------------
AUTOSAR E2E protection for transmitted frames.

Many ECUs (VW/Audi in particular) reject a message unless every
transmission carries a fresh rolling "alive" counter and a CRC over the
payload. A static, repeated frame has a frozen counter and a stale CRC, so
the receiver raises an E2E fault after the first frame.

This module is pure (no bus / asyncio): it computes the protected payload
for a given counter value. The periodic sender lives in e2e_sender.py.

Implemented scheme: AUTOSAR E2E Profile 2, VW/Audi flavour ("vw_p2"):
  - CRC-8H2F: poly 0x2F, init 0xFF, no reflection, xor-out 0xFF
  - counter written into {byte, bit offset, bit width} first
  - CRC input = data[crc_index+1 .. dlc-1] (with the counter written),
    followed by data_id_list[counter & 0x0F]
  - result stored at data[crc_index]
All positions and CRC parameters are configurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Built-in "VW E2E Profile 2" preset
VW_P2_POLY = 0x2F
VW_P2_INIT = 0xFF
VW_P2_XOR_OUT = 0xFF


def crc8h2f(data: bytes) -> int:
    """CRC-8H2F (CRC-8/AUTOSAR). check("123456789") == 0xDF."""
    c = 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x2F) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c ^ 0xFF


def crc8(data: bytes, poly: int = VW_P2_POLY, init: int = VW_P2_INIT,
         xor_out: int = VW_P2_XOR_OUT) -> int:
    """Generic non-reflected CRC-8. Defaults are CRC-8H2F."""
    c = init & 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c ^ (xor_out & 0xFF)


@dataclass(frozen=True)
class E2EConfig:
    data_id_list: list[int] = field(default_factory=lambda: [0] * 16)
    crc_index: int = 0
    counter_byte: int = 1
    counter_bit_offset: int = 0
    counter_width: int = 4
    poly: int = VW_P2_POLY
    init: int = VW_P2_INIT
    xor_out: int = VW_P2_XOR_OUT
    profile: str = "vw_p2"

    @property
    def counter_modulo(self) -> int:
        return 1 << self.counter_width

    def validate(self, dlc: int) -> None:
        """Raise ValueError if the layout cannot work for a frame of `dlc` bytes."""
        if len(self.data_id_list) != 16:
            raise ValueError("dataIdList must contain exactly 16 bytes")
        if any(not 0 <= b <= 0xFF for b in self.data_id_list):
            raise ValueError("dataIdList entries must be bytes (0-255)")
        for name, v in (("poly", self.poly), ("init", self.init), ("xorOut", self.xor_out)):
            if not 0 <= v <= 0xFF:
                raise ValueError(f"crc.{name} must be 0-255")
        if not 1 <= self.counter_width <= 8:
            raise ValueError("counterWidth must be 1-8 bits")
        if not 0 <= self.counter_bit_offset or self.counter_bit_offset + self.counter_width > 8:
            raise ValueError("counter bits must fit inside one byte (offset + width <= 8)")
        if not 0 <= self.crc_index < dlc:
            raise ValueError(f"crcIndex {self.crc_index} is outside a {dlc}-byte frame")
        if not 0 <= self.counter_byte < dlc:
            raise ValueError(f"counterByte {self.counter_byte} is outside a {dlc}-byte frame")
        if self.counter_byte == self.crc_index:
            raise ValueError("counterByte and crcIndex must be different bytes")


def build_e2e_frame(template: bytes, counter: int, data_id_list: list[int],
                    crc_index: int = 0, cnt_byte: int = 1, cnt_off: int = 0,
                    cnt_width: int = 4, poly: int = VW_P2_POLY, init: int = VW_P2_INIT,
                    xor_out: int = VW_P2_XOR_OUT) -> bytes:
    """
    Return `template` with the counter written and the CRC recomputed.
    Signal bits in `template` are preserved; the counter field and CRC byte
    are overwritten.
    """
    d = bytearray(template)
    mask = ((1 << cnt_width) - 1) << cnt_off
    d[cnt_byte] = (d[cnt_byte] & ~mask) | ((counter << cnt_off) & mask)
    crc_in = bytes(d[crc_index + 1:]) + bytes([data_id_list[counter & 0x0F]])
    d[crc_index] = crc8(crc_in, poly, init, xor_out)
    return bytes(d)


def build_with_config(template: bytes, counter: int, cfg: E2EConfig) -> bytes:
    return build_e2e_frame(
        template, counter, cfg.data_id_list,
        crc_index=cfg.crc_index, cnt_byte=cfg.counter_byte,
        cnt_off=cfg.counter_bit_offset, cnt_width=cfg.counter_width,
        poly=cfg.poly, init=cfg.init, xor_out=cfg.xor_out,
    )
