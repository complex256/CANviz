import { useEffect, useRef, useState } from 'react';
import { apiComputePreview, type ComputeConfig, type ComputeStep } from '../../api/client';
import { useSendFrameStore, type TransmitFrame } from '../../store/sendFrameStore';
import { e2ePresetCompute } from '../../store/compute';

const hex2 = (n: number) => n.toString(16).toUpperCase().padStart(2, '0');

function parseHexByte(s: string): number | null {
  const v = parseInt(s.replace(/^0x/i, ''), 16);
  return Number.isNaN(v) || v < 0 || v > 255 ? null : v;
}

function parseTemplate(s: string, dlc: number): number[] {
  const parts = s.replace(/[,\s]+/g, ' ').trim();
  const out = parts ? parts.split(' ').map((h) => parseInt(h, 16) || 0) : [];
  return Array.from({ length: dlc }, (_, i) => out[i] ?? 0);
}

/**
 * Editor for a scripted "compute" frame: an optional rolling counter, a list
 * of sandboxed expression steps, and byte tables. A live preview of the next
 * frame is fetched from the backend (authoritative — no JS evaluator), which
 * also validates the expressions and reports errors inline.
 */
export function ComputeEditor({ frame }: { frame: TransmitFrame }) {
  const updateCompute = useSendFrameStore((s) => s.updateCompute);
  const cfg = frame.compute!;

  const [preview, setPreview] = useState<number[] | null>(null);
  const [error, setError]     = useState<string | null>(null);
  const debounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Fetch a preview (and validation) whenever the template or config changes
  useEffect(() => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(async () => {
      try {
        const data = parseTemplate(frame.data, frame.dlc);
        const res = await apiComputePreview(data, cfg, { id: parseInt(frame.canId.replace(/^0x/i, ''), 16) || 0, dlc: frame.dlc });
        setPreview(res.frames[0] ?? null);
        setError(null);
      } catch (e) {
        setPreview(null);
        setError((e as Error).message);
      }
    }, 150);
    return () => clearTimeout(debounce.current);
  }, [frame.data, frame.dlc, frame.canId, cfg]);

  const patch = (next: Partial<ComputeConfig>) => updateCompute(frame.id, { ...cfg, ...next });

  const setStep = (i: number, next: Partial<ComputeStep>) =>
    patch({ steps: cfg.steps.map((s, j) => (j === i ? { ...s, ...next } : s)) });

  const addStep = () => patch({ steps: [...cfg.steps, { expr: '', target: { byte: 0 } }] });
  const removeStep = (i: number) => patch({ steps: cfg.steps.filter((_, j) => j !== i) });

  const toggleCounter = () =>
    patch({ counter: cfg.counter ? null : { byte: 1, bitOffset: 0, width: 4, step: 1 } });

  const loadPreset = () => updateCompute(frame.id, e2ePresetCompute(cfg.tables.dataId));

  const managed = new Set<number>();
  if (cfg.counter) managed.add(cfg.counter.byte);
  for (const s of cfg.steps) if (s.target) managed.add(s.target.byte);

  return (
    <div style={wrap}>
      {/* Preview row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={fieldLabel}>preview @cnt 0</span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.05em' }}>
          {preview
            ? preview.map((b, i) => (
                <span key={i} style={{ color: managed.has(i) ? 'var(--accent-amber)' : 'var(--text-primary)' }}>
                  {hex2(b)}{' '}
                </span>
              ))
            : <span style={{ color: 'var(--text-muted)' }}>—</span>}
        </span>
        <button style={presetBtn} onClick={loadPreset} title="Fill in the VW E2E Profile 2 pipeline">Load E2E preset</button>
      </div>

      {error && <div style={errStyle}>⚠ {error}</div>}

      {/* Counter */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, flexWrap: 'wrap' }}>
        <label style={{ ...field, flexDirection: 'row', alignItems: 'center', gap: 4 }} title="Write an auto-incrementing counter into a bit field before the steps run">
          <input type="checkbox" checked={!!cfg.counter} onChange={toggleCounter} style={{ accentColor: 'var(--accent-green)' }} />
          <span style={fieldLabel}>counter</span>
        </label>
        {cfg.counter && (
          <>
            {numField('byte', cfg.counter.byte, (v) => patch({ counter: { ...cfg.counter!, byte: v } }), 0, frame.dlc - 1)}
            {numField('bit', cfg.counter.bitOffset, (v) => patch({ counter: { ...cfg.counter!, bitOffset: v } }), 0, 7)}
            {numField('width', cfg.counter.width, (v) => patch({ counter: { ...cfg.counter!, width: v } }), 1, 8)}
            {numField('step', cfg.counter.step, (v) => patch({ counter: { ...cfg.counter!, step: v } }), 1, 255)}
          </>
        )}
      </div>

      {/* Steps */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={fieldLabel}>steps (run top-to-bottom; later steps see earlier writes)</span>
        {cfg.steps.map((step, i) => (
          <div key={i} style={stepRow}>
            {/* target byte / bitfield */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }} title="Target byte for the result (whole byte unless width < 8)">
              <span style={{ ...fieldLabel, fontSize: 9 }}>→B</span>
              <input
                style={{ ...miniInput, width: 30 }}
                value={step.target ? String(step.target.byte) : ''}
                placeholder="—"
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10);
                  setStep(i, { target: Number.isNaN(v) ? null : { byte: v, bitOffset: step.target?.bitOffset ?? 0, width: step.target?.width ?? 8 } });
                }}
              />
            </div>
            {/* expression */}
            <input
              style={{ ...exprInput, flex: 1 }}
              value={step.expr}
              placeholder="crc8(data[1:], 0x2F, 0xFF, 0xFF)"
              spellCheck={false}
              onChange={(e) => setStep(i, { expr: e.target.value })}
            />
            {/* store */}
            <input
              style={{ ...miniInput, width: 54 }}
              value={step.store ?? ''}
              placeholder="store"
              title="Optional: save this step's result into state under this name (read later with mem('name'))"
              onChange={(e) => setStep(i, { store: e.target.value || null })}
            />
            <button style={rmBtn} title="Remove step" disabled={cfg.steps.length <= 1} onClick={() => removeStep(i)}>×</button>
          </div>
        ))}
        <button style={addBtn} onClick={addStep}>+ step</button>
      </div>

      {/* Data-ID table (optional; used by the E2E preset) */}
      {cfg.tables.dataId && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
            <span style={fieldLabel}>dataId table (16 bytes, hex)</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(16, 1fr)', gap: 2 }}>
            {cfg.tables.dataId.map((b, i) => (
              <input
                key={i}
                style={{ ...miniInput, width: '100%' }}
                value={hex2(b)}
                title={`counter ${i}`}
                onChange={(e) => {
                  const v = parseHexByte(e.target.value);
                  if (v === null) return;
                  const list = cfg.tables.dataId.slice();
                  list[i] = v;
                  patch({ tables: { ...cfg.tables, dataId: list } });
                }}
              />
            ))}
          </div>
        </div>
      )}

      <span style={helpText}>
        expressions: <code>data</code>, <code>counter</code>, <code>dlc</code>, tables, and helpers
        (<code>crc8</code>, <code>crc16</code>, <code>sum8</code>, <code>xor8</code>, <code>twos_complement</code>,
        <code> lo/hi</code>, <code>mem</code>). Sandboxed — no imports, attributes or loops.
      </span>
    </div>
  );

  function numField(label: string, value: number, onChange: (v: number) => void, min: number, max: number) {
    return (
      <label style={field}>
        <span style={fieldLabel}>{label}</span>
        <input
          style={miniInput}
          value={String(value)}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10);
            if (Number.isNaN(v)) return;
            onChange(Math.max(min, Math.min(max, v)));
          }}
        />
      </label>
    );
  }
}

const wrap: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 8,
  padding: '6px 2px', borderTop: '1px solid var(--border-subtle)',
};
const field: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 2 };
const fieldLabel: React.CSSProperties = {
  fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', letterSpacing: '0.04em',
};
const miniInput: React.CSSProperties = {
  width: 44, background: 'var(--bg-elevated)', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)', textAlign: 'center',
  fontFamily: 'var(--font-mono)', fontSize: 11, padding: '2px 4px', outline: 'none',
};
const exprInput: React.CSSProperties = {
  background: 'var(--bg-panel)', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)', fontSize: 11, padding: '2px 6px', outline: 'none', minWidth: 0,
};
const stepRow: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 4 };
const addBtn: React.CSSProperties = {
  alignSelf: 'flex-start', background: 'none', border: '1px dashed var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-muted)', fontSize: 10,
  fontFamily: 'var(--font-mono)', cursor: 'pointer', padding: '2px 8px',
};
const rmBtn: React.CSSProperties = {
  background: 'var(--bg-panel)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-muted)', fontSize: 12, cursor: 'pointer', padding: '0 6px', lineHeight: 1.4,
};
const presetBtn: React.CSSProperties = {
  marginLeft: 'auto', background: 'none', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', fontSize: 10,
  fontFamily: 'var(--font-mono)', cursor: 'pointer', padding: '2px 8px',
};
const errStyle: React.CSSProperties = {
  fontSize: 10, fontFamily: 'var(--font-mono)', color: '#ef4444',
};
const helpText: React.CSSProperties = {
  fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', lineHeight: 1.5,
};
