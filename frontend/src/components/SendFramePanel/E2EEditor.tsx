import { useSendFrameStore, type TransmitFrame } from '../../store/sendFrameStore';
import { buildE2EPreview, managedByteIndices } from '../../store/e2e';
import type { E2EConfig } from '../../api/client';

const hex2 = (n: number) => n.toString(16).toUpperCase().padStart(2, '0');

function parseHexByte(s: string): number | null {
  const v = parseInt(s.replace(/^0x/i, ''), 16);
  return Number.isNaN(v) || v < 0 || v > 255 ? null : v;
}

function parseBytes(s: string, dlc: number): number[] {
  const parts = s.replace(/[,\s]+/g, ' ').trim();
  const out = parts ? parts.split(' ').map((h) => parseInt(h, 16) || 0) : [];
  return Array.from({ length: dlc }, (_, i) => out[i] ?? 0);
}

/**
 * E2E config editor for a saved frame. Shows the VW E2E Profile 2 parameters,
 * a 16-byte Data-ID list, and a per-byte view where the counter and CRC bytes
 * are auto-managed (derived, not editable) and the rest are the signal template.
 */
export function E2EEditor({ frame }: { frame: TransmitFrame }) {
  const updateFrame = useSendFrameStore((s) => s.updateFrame);
  const updateE2E   = useSendFrameStore((s) => s.updateE2E);
  const cfg = frame.e2e!;

  const template = parseBytes(frame.data, frame.dlc);
  const managed = managedByteIndices(cfg);
  // Static preview at counter 0 (the "neutral" frame); the live counter runs
  // on the server. This shows where the CRC/counter land and the CRC value.
  const preview = safePreview(template, 0, cfg);

  const setByte = (i: number, v: number) => {
    const next = template.slice();
    next[i] = v;
    updateFrame(frame.id, { data: next.map(hex2).join(' ') });
  };

  const setDataId = (i: number, v: number) => {
    const list = cfg.dataIdList.slice();
    list[i] = v;
    updateE2E(frame.id, { dataIdList: list });
  };

  const num = (label: string, value: number, onChange: (v: number) => void,
               opts?: { min?: number; max?: number; title?: string; hex?: boolean }) => (
    <label style={field} title={opts?.title}>
      <span style={fieldLabel}>{label}</span>
      <input
        style={smallInput}
        value={opts?.hex ? hex2(value) : String(value)}
        onChange={(e) => {
          const v = opts?.hex ? parseHexByte(e.target.value) : parseInt(e.target.value, 10);
          if (v === null || Number.isNaN(v)) return;
          const clamped = Math.max(opts?.min ?? -Infinity, Math.min(opts?.max ?? Infinity, v));
          onChange(clamped);
        }}
      />
    </label>
  );

  return (
    <div style={wrap}>
      {/* Per-byte template with auto-managed counter/CRC */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, flexWrap: 'wrap' }}>
        {template.map((_b, i) => {
          const isManaged = managed.has(i);
          const role = i === cfg.crcIndex ? 'CRC' : i === cfg.counterByte ? 'CNT' : `B${i}`;
          return (
            <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
              <span style={{ ...byteTag, color: isManaged ? 'var(--accent-amber)' : 'var(--text-muted)' }}>{role}</span>
              <input
                style={{
                  ...byteInput,
                  color: isManaged ? 'var(--text-muted)' : 'var(--text-primary)',
                  background: isManaged ? 'var(--bg-panel)' : 'var(--bg-elevated)',
                  borderStyle: isManaged ? 'dashed' : 'solid',
                }}
                value={hex2(preview[i])}
                readOnly={isManaged}
                disabled={isManaged}
                title={isManaged ? `${role} is computed on every send` : `Template byte ${i} (signal bits)`}
                onChange={(e) => { if (!isManaged) { const v = parseHexByte(e.target.value); if (v !== null) setByte(i, v); } }}
              />
            </div>
          );
        })}
        <span style={hint}>counter &amp; CRC auto-managed per send</span>
      </div>

      {/* Profile parameters */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        {num('CRC byte', cfg.crcIndex, (v) => updateE2E(frame.id, { crcIndex: v }), { min: 0, max: frame.dlc - 1, title: 'Index of the CRC byte' })}
        {num('Cnt byte', cfg.counterByte, (v) => updateE2E(frame.id, { counterByte: v }), { min: 0, max: frame.dlc - 1, title: 'Byte holding the alive counter' })}
        {num('Cnt bit', cfg.counterBitOffset, (v) => updateE2E(frame.id, { counterBitOffset: v }), { min: 0, max: 7, title: 'Bit offset of the counter within its byte' })}
        {num('Cnt width', cfg.counterWidth, (v) => updateE2E(frame.id, { counterWidth: v }), { min: 1, max: 8, title: 'Counter width in bits' })}
        {num('Poly', cfg.crc.poly, (v) => updateE2E(frame.id, { crc: { ...cfg.crc, poly: v } }), { min: 0, max: 255, hex: true, title: 'CRC polynomial (0x2F for CRC-8H2F)' })}
        {num('Init', cfg.crc.init, (v) => updateE2E(frame.id, { crc: { ...cfg.crc, init: v } }), { min: 0, max: 255, hex: true })}
        {num('XorOut', cfg.crc.xorOut, (v) => updateE2E(frame.id, { crc: { ...cfg.crc, xorOut: v } }), { min: 0, max: 255, hex: true })}
      </div>

      {/* 16-byte Data-ID list */}
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
          <span style={fieldLabel}>Data-ID list (16 bytes, hex — folded in per counter)</span>
          <button style={pasteBtn} title="Paste 16 space/comma-separated hex bytes" onClick={() => pasteDataIds(frame, updateE2E)}>paste…</button>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(16, 1fr)', gap: 2 }}>
          {cfg.dataIdList.map((b, i) => (
            <input
              key={i}
              style={{ ...byteInput, width: '100%' }}
              value={hex2(b)}
              title={`counter ${i}`}
              onChange={(e) => { const v = parseHexByte(e.target.value); if (v !== null) setDataId(i, v); }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// counterModuloValue isn't a real field; keep preview robust regardless
function safePreview(template: number[], counter: number, cfg: E2EConfig): number[] {
  try {
    return buildE2EPreview(template, counter, cfg);
  } catch {
    return template;
  }
}

function pasteDataIds(frame: TransmitFrame, updateE2E: (id: string, patch: Partial<E2EConfig>) => void) {
  const raw = window.prompt('Paste 16 hex bytes for the Data-ID list:',
    frame.e2e!.dataIdList.map(hex2).join(' '));
  if (raw === null) return;
  const bytes = raw.replace(/[,\s]+/g, ' ').trim().split(' ')
    .map((h) => parseInt(h, 16)).filter((v) => !Number.isNaN(v));
  if (bytes.length !== 16) { window.alert(`Expected 16 bytes, got ${bytes.length}`); return; }
  updateE2E(frame.id, { dataIdList: bytes.map((v) => v & 0xff) });
}

const wrap: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 8,
  padding: '6px 2px', borderTop: '1px solid var(--border-subtle)',
};
const byteTag: React.CSSProperties = {
  fontSize: 8, fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
};
const byteInput: React.CSSProperties = {
  width: 30, textAlign: 'center',
  background: 'var(--bg-elevated)', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)', fontSize: 11, padding: '2px 0', outline: 'none',
};
const field: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 2 };
const fieldLabel: React.CSSProperties = {
  fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', letterSpacing: '0.04em',
};
const smallInput: React.CSSProperties = {
  width: 48, background: 'var(--bg-elevated)', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)', fontSize: 11, padding: '2px 4px', outline: 'none',
};
const hint: React.CSSProperties = {
  fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', marginLeft: 4, marginBottom: 4,
};
const pasteBtn: React.CSSProperties = {
  background: 'none', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-muted)', fontSize: 9, fontFamily: 'var(--font-mono)', cursor: 'pointer', padding: '1px 6px',
};
