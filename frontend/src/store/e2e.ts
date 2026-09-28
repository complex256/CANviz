import type { E2EConfig } from '../api/client';

// Built-in "VW E2E Profile 2" preset (AUTOSAR E2E Profile 2, VW/Audi flavour).
// CRC-8H2F, counter in byte1 low nibble, Data-ID folded in per counter.
export const VW_P2_PRESET: E2EConfig = {
  profile: 'vw_p2',
  crcIndex: 0,
  counterByte: 1,
  counterBitOffset: 0,
  counterWidth: 4,
  crc: { poly: 0x2f, init: 0xff, xorOut: 0xff },
  dataIdList: Array(16).fill(0),
};

export function defaultE2EConfig(): E2EConfig {
  return { ...VW_P2_PRESET, crc: { ...VW_P2_PRESET.crc }, dataIdList: Array(16).fill(0) };
}

/** Generic non-reflected CRC-8. Defaults are CRC-8H2F. Mirrors backend canviz.e2e.crc8. */
export function crc8(data: number[], poly = 0x2f, init = 0xff, xorOut = 0xff): number {
  let c = init & 0xff;
  for (const b of data) {
    c ^= b & 0xff;
    for (let i = 0; i < 8; i++) {
      c = c & 0x80 ? ((c << 1) ^ poly) & 0xff : (c << 1) & 0xff;
    }
  }
  return c ^ (xorOut & 0xff);
}

/**
 * Preview the protected frame for a given counter — the same computation the
 * backend performs on each transmission. Used only to show the derived
 * counter/CRC bytes in the editor; the backend remains authoritative.
 */
export function buildE2EPreview(template: number[], counter: number, cfg: E2EConfig): number[] {
  const d = template.slice();
  const mask = ((1 << cfg.counterWidth) - 1) << cfg.counterBitOffset;
  d[cfg.counterByte] = (d[cfg.counterByte] & ~mask) | ((counter << cfg.counterBitOffset) & mask);
  const idByte = cfg.dataIdList[counter & 0x0f] ?? 0;
  const crcIn = [...d.slice(cfg.crcIndex + 1), idByte];
  d[cfg.crcIndex] = crc8(crcIn, cfg.crc.poly, cfg.crc.init, cfg.crc.xorOut);
  return d;
}

/** Byte indices the E2E layer overwrites on every send (shown as auto-managed). */
export function managedByteIndices(cfg: E2EConfig): Set<number> {
  return new Set([cfg.crcIndex, cfg.counterByte]);
}

/** Parse/normalise an "e2e" object from a saved file, preserving unknown keys. */
export function parseE2EConfig(v: unknown): E2EConfig | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  const base = defaultE2EConfig();
  const num = (x: unknown, d: number) => (typeof x === 'number' && Number.isFinite(x) ? x : d);
  const crc = (o.crc && typeof o.crc === 'object' ? o.crc : {}) as Record<string, unknown>;
  const rawList = Array.isArray(o.dataIdList) ? o.dataIdList : base.dataIdList;
  const dataIdList = Array.from({ length: 16 }, (_, i) => num(rawList[i], 0) & 0xff);
  return {
    ...o,                                   // keep unknown fields for round-trip
    profile: typeof o.profile === 'string' ? o.profile : base.profile,
    crcIndex: num(o.crcIndex, base.crcIndex),
    counterByte: num(o.counterByte, base.counterByte),
    counterBitOffset: num(o.counterBitOffset, base.counterBitOffset),
    counterWidth: num(o.counterWidth, base.counterWidth),
    crc: {
      poly: num(crc.poly, base.crc.poly),
      init: num(crc.init, base.crc.init),
      xorOut: num(crc.xorOut, base.crc.xorOut),
    },
    dataIdList,
  };
}
