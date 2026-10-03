/** Font picker. Per role, because a page face and a UI face want different
 *  things, and a different face per layer means the eye knows whether it is
 *  reading source or manuscript before it reads a word.
 *
 *  The preview renders real prose from the open scene rather than a pangram:
 *  these faces look fine in eight words and reveal their rhythm only in eight
 *  lines.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { BUNDLED, SUGGESTED_SYSTEM, missingGlyphs, resolves } from '../lib/faces'
import type { PageType } from '../lib/typography'
import type { FontRegistry, RoleSetting } from '../types'

interface Props {
  fonts: FontRegistry | null
  openRole: string
  effective: PageType
  sample: string
  onChange: (fonts: FontRegistry) => void
  onClose: () => void
}

const ROLES: { key: string; label: string; hint: string }[] = [
  { key: 'page', label: 'page', hint: 'the manuscript — and the default every other role follows' },
  { key: 'log', label: 'log layer', hint: 'give the source its own face, so the layer is obvious' },
  { key: 'ui', label: 'chrome', hint: 'rail, panes, labels' },
  { key: 'diff', label: 'diff', hint: 'the fork pane' },
]

export function TypographyPanel({
  fonts,
  openRole,
  effective,
  sample,
  onChange,
  onClose,
}: Props) {
  const [role, setRole] = useState(openRole)
  const [family, setFamily] = useState('')
  const [licence, setLicence] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const setting: RoleSetting = fonts?.roles?.[role] ?? {
    family: '',
    size: 0,
    line_height: 0,
    measure: 0,
    letter_spacing: 0,
  }

  // Memoised on the registry rather than recomputed: this list feeds an effect
  // that registers FontFace objects, and a fresh array each render would
  // re-register every imported face on every keystroke.
  const userFaces = useMemo(() => fonts?.faces ?? [], [fonts])
  const options = useMemo(() => {
    const bundled = BUNDLED.map((f) => ({
      family: f.family,
      licence: f.licence,
      kind: 'bundled' as const,
      italic: f.italic,
      note: f.note,
      available: true,
    }))
    const user = userFaces.map((f) => ({
      family: f.family,
      licence: f.licence,
      kind: f.kind,
      italic: true,
      note: f.kind === 'system' ? 'installed on this machine' : 'imported file',
      available: f.available,
    }))
    return [...bundled, ...user]
  }, [userFaces])

  // Load imported faces so a preview can actually show them.
  useEffect(() => {
    for (const face of userFaces) {
      if (face.kind !== 'imported' || !face.url) continue
      const loaded = new FontFace(face.family, `url(${face.url})`)
      loaded
        .load()
        .then((f) => document.fonts.add(f))
        .catch(() => undefined)
    }
  }, [userFaces])

  const patch = async (fields: Record<string, unknown>) => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.setFontRole(role, { ...setting, ...fields })
      onChange(res.fonts)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const addSystem = async () => {
    if (!family.trim()) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.addSystemFont(family.trim(), licence.trim())
      onChange(res.fonts)
      setFamily('')
      setLicence('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const importFile = async (file: File) => {
    setBusy(true)
    setError(null)
    try {
      onChange(await api.importFont(file, licence.trim()))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const probeFamily = family.trim()
  const probeResolved = probeFamily ? resolves(probeFamily) : null

  // What this role renders as, which is its own setting or whatever it inherits.
  // Showing only the role's own value would leave every control blank on a role
  // that is following the page, as if nothing were set anywhere.
  const activeFamily = setting.family || (role === openRole ? effective.family : '')
  const metrics = {
    size: setting.size || (role === openRole ? effective.size : 16),
    lineHeight: setting.line_height || (role === openRole ? effective.lineHeight : 1.75),
    measure: setting.measure || (role === openRole ? effective.measure : 68),
    tracking: setting.letter_spacing || (role === openRole ? effective.tracking : 0),
  }
  const gaps = activeFamily ? missingGlyphs(activeFamily) : []

  return (
    <div className="sheet" role="dialog" aria-label="typography">
      <header className="sheet-head">
        <h3>typography</h3>
        <button className="chip ghost" onClick={onClose}>
          ✕
        </button>
      </header>

      <div className="sheet-body">
        <div className="row wrap">
          {ROLES.map((r) => (
            <button
              key={r.key}
              className={`chip ${role === r.key ? 'is-on' : ''}`}
              onClick={() => setRole(r.key)}
              title={r.hint}
            >
              {r.label}
              {r.key === openRole ? ' \u25cf' : ''}
            </button>
          ))}
        </div>
        <p className="hint">
          {role === openRole
            ? `On screen now: ${effective.family}`
            : `Editing the ${role} role — the layer on screen reads ${openRole}.`}
          {role === 'log' && effective.inherited && !setting.family
            ? ' The log has no face of its own and follows the page.'
            : ''}
        </p>

        <section className="field">
          <h4>family</h4>
          {role !== 'page' && (
            <div className="row">
              <button
                className={`chip ${!setting.family ? 'is-on' : ''}`}
                disabled={busy || !setting.family}
                onClick={() => patch({ family: '' })}
                title="use whatever the page role is set to"
              >
                follow page
              </button>
            </div>
          )}
          <ul className="face-list">
            {options.map((f) => (
              <li key={`${f.kind}:${f.family}`}>
                <button
                  className={`face ${activeFamily === f.family ? 'is-on' : ''}`}
                  disabled={busy}
                  onClick={() => patch({ family: f.family })}
                  style={{ fontFamily: `"${f.family}", var(--font-mono)` }}
                >
                  <span className="face-name">{f.family}</span>
                  <span className="face-meta">
                    <span className={`licence ${f.kind}`}>{f.licence}</span>
                    {!f.italic && <span className="warn">no italic — accents only</span>}
                    {f.available === false && <span className="warn">file missing</span>}
                  </span>
                  <span className="face-note">{f.note}</span>
                </button>
              </li>
            ))}
          </ul>
          {activeFamily && gaps.length > 0 && (
            <p className="warn small">
              {activeFamily} lacks {gaps.join(' ')} — those will fall back to another
              face mid-page
            </p>
          )}
        </section>

        <section className="field">
          <h4>metrics</h4>
          <Slider
            label="size"
            value={metrics.size}
            min={11}
            max={26}
            step={0.5}
            unit="px"
            onChange={(v) => patch({ size: v })}
          />
          <Slider
            label="leading"
            value={metrics.lineHeight}
            min={1.2}
            max={2.4}
            step={0.05}
            unit=""
            onChange={(v) => patch({ line_height: v })}
          />
          <Slider
            label="measure"
            value={metrics.measure}
            min={40}
            max={110}
            step={1}
            unit="ch"
            onChange={(v) => patch({ measure: v })}
          />
          <Slider
            label="tracking"
            value={metrics.tracking}
            min={-0.02}
            max={0.08}
            step={0.005}
            unit="em"
            onChange={(v) => patch({ letter_spacing: v })}
          />
        </section>

        <section className="field">
          <h4>preview</h4>
          <div
            className="type-preview"
            style={{
              fontFamily: `"${activeFamily || 'Courier Prime'}", var(--font-mono)`,
              fontSize: `${metrics.size}px`,
              lineHeight: metrics.lineHeight,
              letterSpacing: `${metrics.tracking}em`,
              maxWidth: `${metrics.measure}ch`,
            }}
          >
            {sample || 'Open a scene to preview real prose in this face.'}
          </div>
        </section>

        <section className="field">
          <h4>use a font installed on this machine</h4>
          <p className="hint">
            Nothing is copied — CSS resolves the name against the OS, so a face
            licensed for personal use stays perfectly legal to read with. Browsers
            will not list installed fonts, so type the family name and the preview
            below says whether it resolved.
          </p>
          <div className="row">
            <input
              value={family}
              placeholder="Traveling Typewriter"
              onChange={(e) => setFamily(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addSystem()}
            />
            <button className="btn" onClick={addSystem} disabled={busy || !probeFamily}>
              add
            </button>
          </div>
          <input
            value={licence}
            placeholder="licence, e.g. personal use — display only"
            onChange={(e) => setLicence(e.target.value)}
          />
          {probeFamily && (
            <p className={probeResolved ? 'hint ok' : 'warn small'}>
              {probeResolved
                ? `${probeFamily} resolved`
                : `${probeFamily} did not resolve — install it, or check the exact family name`}
            </p>
          )}
          <div className="row wrap">
            {SUGGESTED_SYSTEM.map((s) => (
              <button
                key={s.family}
                className="chip tiny"
                onClick={() => {
                  setFamily(s.family)
                  setLicence(s.licence)
                }}
              >
                {s.family}
              </button>
            ))}
          </div>
        </section>

        <section className="field">
          <h4>import a font file</h4>
          <p className="hint">
            For a face you want to travel with the project. It is copied to the
            workspace, which is gitignored, and never embedded in an export —
            published text carries no fonts at all.
          </p>
          <input
            ref={fileRef}
            type="file"
            accept=".ttf,.otf,.woff,.woff2"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void importFile(file)
              if (fileRef.current) fileRef.current.value = ''
            }}
          />
        </section>

        {userFaces.length > 0 && (
          <section className="field">
            <h4>your faces</h4>
            <ul className="face-list compact">
              {userFaces.map((f) => (
                <li key={f.family}>
                  <span className="face-name">{f.family}</span>
                  <span className={`licence ${f.kind}`}>{f.kind} · {f.licence}</span>
                  <button
                    className="chip ghost tiny"
                    disabled={busy}
                    onClick={async () => onChange((await api.removeFont(f.family)).fonts)}
                  >
                    forget
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        {error && <p className="error">{error}</p>}
      </div>
    </div>
  )
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  unit: string
  onChange: (v: number) => void
}) {
  const [local, setLocal] = useState(value)
  useEffect(() => setLocal(value), [value])
  return (
    <label className="slider">
      <span>{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={local}
        onChange={(e) => setLocal(Number(e.target.value))}
        onMouseUp={() => onChange(local)}
        onKeyUp={() => onChange(local)}
      />
      <output>
        {local}
        {unit}
      </output>
    </label>
  )
}
