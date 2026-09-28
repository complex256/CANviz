"""
canviz/models.py
----------------
Pydantic models used across REST endpoints and WebSocket messages.
Keeping them in one place avoids circular imports.
"""

from __future__ import annotations
from typing import Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class CANFrame(BaseModel):
    """A single CAN frame as it travels over the WebSocket."""
    id: str                        # Hex string e.g. "0x1FF"
    dlc: int                       # Data length code (0-8)
    data: list[int]                # Raw bytes as ints
    timestamp: float               # Seconds since bus open
    is_extended_id: bool = False
    is_fd: bool = False            # True for CAN FD frames (Phase 4)
    channel: int = 0
    signals: dict[str, float] = {}  # Populated if a DBC is loaded


class ConnectionStatus(BaseModel):
    connected: bool
    interface: str
    channel: str
    bitrate: int
    index: int
    error: Optional[str] = None


class ConnectRequest(BaseModel):
    interface: str = "gs_usb"
    # channel is a string for slcan/socketcan (e.g. "COM3", "can0")
    # and an int for gs_usb (device index).
    # Accept both; bus.py passes it as `index` for gs_usb.
    channel: Union[str, int] = ""
    bitrate: int = 500_000
    baudrate: int = 115_200
    index: int = 0


class SendFrameRequest(BaseModel):
    id: int                        # Arbitration ID as integer
    data: list[int]                # Up to 8 bytes
    is_extended_id: bool = False


class E2ECrcParams(BaseModel):
    poly: int = 0x2F
    init: int = 0xFF
    xor_out: int = Field(0xFF, alias="xorOut")

    model_config = ConfigDict(populate_by_name=True)


class E2EConfigModel(BaseModel):
    """Same shape as the optional "e2e" object in saved frame sets (camelCase)."""
    profile: str = "vw_p2"
    crc_index: int = Field(0, alias="crcIndex")
    counter_byte: int = Field(1, alias="counterByte")
    counter_bit_offset: int = Field(0, alias="counterBitOffset")
    counter_width: int = Field(4, alias="counterWidth")
    crc: E2ECrcParams = E2ECrcParams()
    data_id_list: list[int] = Field(alias="dataIdList")

    model_config = ConfigDict(populate_by_name=True)


class E2EFrameRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)  # client-chosen job id
    id: int                        # Arbitration ID as integer
    data: list[int]                # Template: signal bits set, counter/CRC overwritten
    dlc: Optional[int] = None      # Defaults to len(data); template is zero-padded to it
    is_extended_id: bool = False
    e2e: E2EConfigModel


class E2EStartRequest(E2EFrameRequest):
    interval_ms: int = Field(ge=1, le=60_000)


class E2EStopRequest(BaseModel):
    key: str
    forget: bool = False           # also drop the counter (frame deleted in the UI)


class DBCEncodeRequest(BaseModel):
    message: str                   # DBC message name
    signals: dict[str, float] = {} # Physical values; missing signals use DBC initial value


class DBCDecodeRequest(BaseModel):
    message: str                   # DBC message name
    data: list[int] = Field(max_length=64)  # Raw bytes


class DBCInfo(BaseModel):
    message_count: int
    messages: list[dict]           # name, id, signals[]