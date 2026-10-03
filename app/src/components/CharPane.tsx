/** The Char tab: who is in the story, and where.
 *
 * The roster lists everyone with a bible — the main cast from their cards and
 * the characters filed automatically from play — with how often each is voiced,
 * is the point of view, or is only named. Opening one shows their bible, their
 * dossier and every scene they are in; a scene row jumps the page there.
 */

import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import type { CastMember, CastRow, CastView } from '../types'

const ORIGIN_LABEL: Record<string, string> = {
  cards: 'Main cast',
  'player-mode': 'Filed from play',
}

function Portrait({ who, name, size }: { who: string; name: string; size: number }) {
  const [failed, setFailed] = useState(false)
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0] ?? '')
    .join('')
    .toUpperCase()
  if (failed) {
    return (
      <span className="cast-monogram" style={{ width: size, height: size, fontSize: size * 0.38 }}>
        {initials}
      </span>
    )
  }
  return (
    <img
      className="cast-portrait"
      src={`/portrait/${encodeURIComponent(who)}`}
      alt=""
      width={size}
      height={size}
      onError={() => setFailed(true)}
    />
  )
}

function Counts({ c }: { c: CastRow['counts'] }) {
  const bits = [
    c.voiced ? `voiced ${c.voiced}` : '',
    c.pov ? `POV ${c.pov}` : '',
    c.mentioned ? `named ${c.mentioned}` : '',
  ].filter(Boolean)
  return <span className="muted small">{bits.length ? bits.join(' · ') : 'not in this log yet'}</span>
}

export function CharPane({ onJump }: { onJump: (msgId: number) => void }) {
  const [view, setView] = useState<CastView | null>(null)
  const [open, setOpen] = useState<CastMember | null>(null)
  const [filter, setFilter] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setView(await api.cast())
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const openMember = async (key: string) => {
    setError(null)
    try {
      setOpen(await api.castMember(key))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const groups = useMemo(() => {
    const rows = (view?.characters ?? []).filter((r) => {
      const q = filter.trim().toLowerCase()
      return !q || r.name.toLowerCase().includes(q) || r.aliases.some((a) => a.toLowerCase().includes(q))
    })
    const out = new Map<string, CastRow[]>()
    for (const r of rows) {
      const g = ORIGIN_LABEL[r.origin] ?? r.origin
      out.set(g, [...(out.get(g) ?? []), r])
    }
    return [...out.entries()]
  }, [view, filter])

  if (open) {
    const m = open
    return (
      <div className="slot-body cast">
        <button className="link small" onClick={() => setOpen(null)}>
          ← cast
        </button>
        <div className="cast-head">
          <Portrait who={m.key} name={m.name} size={64} />
          <div>
            <h3 className="cast-name">{m.name}</h3>
            {m.label && m.label !== m.name && <p className="small muted">{m.label}</p>}
            <p className="small">
              <span className={`badge ${m.origin === 'player-mode' ? 'proposed' : ''}`}>
                {ORIGIN_LABEL[m.origin] ?? m.origin}
              </span>{' '}
              <Counts c={m.counts} />
            </p>
          </div>
        </div>
        {m.role && <p className="small">{m.role}</p>}
        {m.aliases.length > 1 && <p className="small muted">also: {m.aliases.filter((a) => a !== m.name).join(', ')}</p>}

        {m.canonical_facts.length > 0 && (
          <details open>
            <summary>facts</summary>
            <ul className="cast-list">{m.canonical_facts.map((f, i) => <li key={i}>{f}</li>)}</ul>
          </details>
        )}
        {m.voice_rules.length > 0 && (
          <details>
            <summary>voice</summary>
            <ul className="cast-list">{m.voice_rules.map((f, i) => <li key={i}>{f}</li>)}</ul>
          </details>
        )}
        {Object.keys(m.relationship_notes).length > 0 && (
          <details>
            <summary>relationships</summary>
            <ul className="cast-list">
              {Object.entries(m.relationship_notes).map(([who, note]) => (
                <li key={who}>
                  <strong>{who}</strong>: {note}
                </li>
              ))}
            </ul>
          </details>
        )}
        {m.forbidden_phrasings.length > 0 && (
          <details>
            <summary>never says</summary>
            <ul className="cast-list">{m.forbidden_phrasings.map((f, i) => <li key={i}>{f}</li>)}</ul>
          </details>
        )}
        {m.dossier.length > 0 && (
          <details>
            <summary>dossier ({m.dossier.length})</summary>
            <ul className="cast-list">
              {m.dossier.map((e) => (
                <li key={e.id}>
                  <span className="muted small">{e.kind}</span> {e.text}
                </li>
              ))}
            </ul>
          </details>
        )}

        <h4 className="cast-section">in {m.spans.length} scene{m.spans.length === 1 ? '' : 's'}</h4>
        {m.spans.length === 0 && <p className="small muted">Not in this log yet.</p>}
        <ul className="cast-scenes">
          {m.spans.map((s) => (
            <li key={`${s.start}-${s.end}`}>
              <button className={`cast-scene how-${s.how}`} onClick={() => onJump(s.jump)}
                title={`jump to msg ${s.jump}`}>
                <span className="cast-scene-name">{s.scene || `msgs ${s.start}–${s.end}`}</span>
                <span className="muted small">
                  {s.start === s.end ? `msg ${s.start}` : `msgs ${s.start}–${s.end}`}
                  {s.counts.voiced ? ` · voiced ${s.counts.voiced}` : ''}
                  {s.counts.pov ? ` · POV ${s.counts.pov}` : ''}
                  {s.counts.mentioned ? ` · named ${s.counts.mentioned}` : ''}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {error && <p className="error small">{error}</p>}
      </div>
    )
  }

  return (
    <div className="slot-body cast">
      <div className="cast-toolbar">
        <input
          className="cast-filter"
          placeholder="find a character"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button className="chip ghost" onClick={() => void load()} disabled={loading} title="recount">
          {loading ? '…' : 'refresh'}
        </button>
      </div>
      {view && (
        <p className="muted small">
          {view.characters.length} characters · {view.messages} messages
        </p>
      )}
      {groups.map(([group, rows]) => (
        <section key={group}>
          <h4 className="cast-section">{group}</h4>
          <ul className="cast-roster">
            {rows.map((r) => (
              <li key={r.key}>
                <button className="cast-row" onClick={() => void openMember(r.key)}>
                  <Portrait who={r.key} name={r.name} size={32} />
                  <span className="cast-row-text">
                    <span className="cast-row-name">{r.name}</span>
                    {r.role && <span className="small muted cast-row-role">{r.role}</span>}
                    <Counts c={r.counts} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {view && view.characters.length === 0 && (
        <p className="small muted">
          No character bibles yet. They live in <code>canon/characters.json</code>; characters introduced
          in Player mode are filed there when a play log is imported.
        </p>
      )}
      {error && <p className="error small">{error}</p>}
    </div>
  )
}
