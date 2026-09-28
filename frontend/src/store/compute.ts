import type { ComputeConfig, ComputeStep, ComputeTarget } from '../api/client';
import { VW_P2_PRESET } from './e2e';

export function defaultComputeConfig(): ComputeConfig {
  return {
    version: 1,
    counter: null,
    steps: [{ expr: '', target: { byte: 0 } }],
    tables: {},
  };
}

/**
 * The AUTOSAR E2E Profile 2 (VW/Audi) transform expressed as a compute
 * pipeline — the "Load E2E preset" button. Equivalent to the built-in E2E
 * frame, but editable.
 */
export function e2ePresetCompute(dataIdList?: number[]): ComputeConfig {
  const p = VW_P2_PRESET;
  return {
    version: 1,
    counter: { byte: p.counterByte, bitOffset: p.counterBitOffset, width: p.counterWidth, step: 1 },
    steps: [{
      target: { byte: p.crcIndex },
      expr: `crc8(data[${p.crcIndex + 1}:] + [dataId[counter]], 0x2F, 0xFF, 0xFF)`,
    }],
    tables: { dataId: (dataIdList ?? Array(16).fill(0)).slice() },
  };
}

function parseTarget(v: unknown): ComputeTarget | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.byte !== 'number') return null;
  return {
    byte: o.byte,
    bitOffset: typeof o.bitOffset === 'number' ? o.bitOffset : 0,
    width: typeof o.width === 'number' ? o.width : 8,
  };
}

function parseStep(v: unknown): ComputeStep | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.expr !== 'string') return null;
  return {
    expr: o.expr,
    target: parseTarget(o.target),
    store: typeof o.store === 'string' && o.store ? o.store : null,
  };
}

/** Tolerant parse of a saved "compute" object, preserving unknown keys. */
export function parseComputeConfig(v: unknown): ComputeConfig | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (!Array.isArray(o.steps)) return null;
  const steps = o.steps.map(parseStep).filter((s): s is ComputeStep => s !== null);
  if (steps.length === 0) return null;

  const rawCounter = o.counter as Record<string, unknown> | null | undefined;
  const counter = rawCounter && typeof rawCounter.byte === 'number'
    ? {
        byte: rawCounter.byte,
        bitOffset: typeof rawCounter.bitOffset === 'number' ? rawCounter.bitOffset : 0,
        width: typeof rawCounter.width === 'number' ? rawCounter.width : 8,
        step: typeof rawCounter.step === 'number' ? rawCounter.step : 1,
      }
    : null;

  const tables: Record<string, number[]> = {};
  if (o.tables && typeof o.tables === 'object') {
    for (const [k, val] of Object.entries(o.tables as Record<string, unknown>)) {
      if (Array.isArray(val)) tables[k] = val.map((n) => (typeof n === 'number' ? n & 0xff : 0));
    }
  }

  return {
    ...o,                              // round-trip any unknown keys
    version: typeof o.version === 'number' ? o.version : 1,
    counter,
    steps,
    tables,
  };
}
