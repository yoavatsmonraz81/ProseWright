/** The Play view: play the story with the Player-mode engine, in the editor.
 *
 * The transcript is the play folder's log; each move runs one turn on the
 * engine (planner + narrator, about 15–20 seconds) and the new messages are
 * added to the project's working log straight away, labelled with who they
 * involve — so a played scene is ready to read, edit and novelize.
 */

import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { PlayView } from '../types'

/** *italics* in the engine's prose, as the narrator writes them. */
function Prose({ text }: { text: string }) {
  const body = text.replace(/^\s*\[[^\]\n]*\]\s*\n?/, '')
  return (
    <>
      {body.split(/\n\s*\n/).map((para, i) => (
        <p key={i}>
          {para.split(/(\*[^*\n]+\*)/g).map((bit, j) =>
            bit.startsWith('*') && bit.endsWith('*') && bit.length > 2 ? (
              <em key={j}>{bit.slice(1, -1)}</em>
            ) : (
              <span key={j}>{bit}</span>
            ),
          )}
        </p>
      ))}
    </>
  )
}

export function PlayPane({ onLogChanged }: { onLogChanged: () => void }) {
  const [view, setView] = useState<PlayView | null>(null)
  const [move, setMove] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [note, setNote] = useState('')
  const [debug, setDebug] = useState(false)
  const [debugLines, setDebugLines] = useState<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  const load = async () => {
    try {
      setView(await api.play())
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  useEffect(() => {
    void load()
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [view?.rows.length, busy])

  useEffect(() => {
    if (!busy) return
    setElapsed(0)
    const t = window.setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => window.clearInterval(t)
  }, [busy])

  async function act(label: string, work: () => Promise<{ view: PlayView } & Record<string, unknown>>) {
    setBusy(label)
    setError(null)
    setNote('')
    try {
      const result = await work()
      setView(result.view)
      const synced = result.synced as { added?: number; filed?: string[] } | undefined
      const bits: string[] = []
      if (synced?.added) bits.push(`${synced.added} message${synced.added === 1 ? '' : 's'} added to the log`)
      if (synced?.filed?.length) bits.push(`filed: ${synced.filed.join(', ')}`)
      if (typeof result.message === 'string') bits.push(result.message)
      if (typeof result.removed === 'number' && result.removed) bits.push(`${result.removed} taken out of the log`)
      if (typeof result.retold === 'number' && result.retold) bits.push('the log has the new telling')
      if (typeof result.kept_edited === 'number' && result.kept_edited)
        bits.push(`${result.kept_edited} kept in the log (edited there)`)
      setNote(bits.join(' · '))
      setPrompt(typeof result.prompt === 'string' ? result.prompt : '')
      setDebugLines(Array.isArray(result.debug) ? (result.debug as string[]) : [])
      onLogChanged()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const send = () => {
    const text = move.trim()
    if (!text || busy) return
    void act('the world is moving', async () => {
      const result = await api.playTurn(text, debug)
      setMove('')
      return result
    })
  }

  if (!view) {
    return (
      <div className="page play-page">
        <div className="play-sheet"><p className="muted">{error ?? 'Opening the play folder…'}</p></div>
      </div>
    )
  }
  if (!view.configured) {
    return (
      <div className="page play-page">
        <div className="play-sheet">
          <p>This project has no play folder.</p>
          <p className="muted small">
            Name one in its <code>project.json</code>:{' '}
            <code>"integrations": {'{'} "player_mode": {'{'} "home": "/path/to/play" {'}'} {'}'}</code>. A play
            folder holds the story's world, scene cards and cast for the Player-mode engine.
          </p>
        </div>
      </div>
    )
  }

  const s = view.status
  const pendingCut = s.pending?.type === 'cut'
  const lastRow = view.rows[view.rows.length - 1]
  const swipeable = !!lastRow && !lastRow.is_user && lastRow.kind === 'narration'
  let lastScene = ''

  return (
    <div className="page play-page">
      <div className="play-sheet">
        {view.rows.length === 0 && (
          <div className="play-empty">
            <p>Nothing played yet.</p>
            <button className="btn" disabled={!!busy} onClick={() => void act('setting the scene', () => api.playBegin(debug))}>
              begin
            </button>
          </div>
        )}
        {view.rows.map((r) => {
          const divider = r.scene && r.scene !== lastScene
          lastScene = r.scene || lastScene
          return (
            <div key={r.uid}>
              {divider && <h3 className="play-scene">{r.scene_title || r.scene}</h3>}
              {r.is_user ? (
                <div className="play-move">
                  <span className="play-who">{r.name}</span>
                  <Prose text={r.text} />
                </div>
              ) : r.kind === 'summary' ? (
                <div className="play-summary"><Prose text={r.text} /></div>
              ) : (
                <div className="play-narration">
                  <Prose text={r.text} />
                  {swipeable && r.uid === lastRow.uid && (
                    <div className="play-swipe">
                      {r.swipes > 1 && (
                        <span className="play-swipe-nav">
                          <button className="link" disabled={!!busy || r.swipe_id <= 0}
                            onClick={() => void act('switching telling', () => api.playSwipe(r.swipe_id - 1))}>
                            ‹
                          </button>
                          {r.swipe_id + 1}/{r.swipes}
                          <button className="link" disabled={!!busy || r.swipe_id >= r.swipes - 1}
                            onClick={() => void act('switching telling', () => api.playSwipe(r.swipe_id + 1))}>
                            ›
                          </button>
                        </span>
                      )}
                      <button className="link" disabled={!!busy}
                        title="the same events, written again (about half a turn's cost); every telling is kept"
                        onClick={() => void act('retelling', () => api.playRenarrate(debug))}>
                        retell
                      </button>
                      <button className="link" disabled={!!busy}
                        title="play this turn again from scratch: a new plan, other events (a full turn's cost); the old one is discarded"
                        onClick={() => void act('re-planning', () => api.playReroll(debug))}>
                        re-plan
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {busy && <p className="play-busy">{busy}… {elapsed}s</p>}
        <div ref={endRef} />
      </div>

      <div className="play-dock">
        <div className="play-status">
          <span className="play-status-title">{s.title}</span>
          {s.question && <span className="muted"> · “{s.question}”</span>}
          <span className="muted">
            {' '}· rung {s.rung}/{s.rungs} · move {s.moves} (dwell {s.dwell})
          </span>
          <span className="play-stage">
            {' '}in the air: {s.stage.length ? s.stage.map((x) => `● ${x}`).join(' ') : '–'}
            {s.mystery ? ` · mystery: ${s.mystery}` : ''}
          </span>
          <span
            className="muted"
            title="What the planner and narrator calls would cost at API list prices: everything played in this play folder, and today's turns. On a Claude subscription turns count against your plan's usage, not money."
          >
            {' '}· ≈ ${s.cost.toFixed(2)} so far · ${s.cost_today.toFixed(2)} today (list price)
          </span>
        </div>

        {pendingCut && (
          <div className="play-banner">
            <span>The scene can end here.{s.pending?.summary ? ` ${s.pending.summary}` : ''}</span>
            <button className="btn" disabled={!!busy} onClick={() => void act('cutting to the next scene', () => api.playTurn('y', debug))}>
              cut to the next scene
            </button>
            <button className="btn" disabled={!!busy} onClick={() => void act('staying', () => api.playTurn('n', debug))}>
              stay
            </button>
          </div>
        )}
        {prompt && !pendingCut && <div className="play-banner"><span>{prompt.replace(/^⟡\s*/gm, '')}</span></div>}

        <div className="play-input">
          <textarea
            value={move}
            placeholder={`What does ${s.pov_name} do? (Enter to play, Shift+Enter for a new line)`}
            disabled={!!busy || view.rows.length === 0}
            onChange={(e) => setMove(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            rows={3}
          />
          <div className="play-actions">
            <button className="btn" disabled={!!busy || !move.trim()} onClick={send}>
              play
            </button>
            <button className="btn" disabled={!!busy || !view.can_undo}
              onClick={() => void act('undoing', () => api.playUndo())}
              title="take back the last turn (and its messages in the log, unless you edited them there)">
              undo
            </button>
            <label className="small muted" title="whose eyes the story follows">
              as{' '}
              <select
                value={s.pov}
                disabled={!!busy}
                onChange={(e) => void act('switching', () => api.playAs(e.target.value))}
              >
                {view.cast.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </label>
            <label className="small muted">
              <input type="checkbox" checked={debug} onChange={(e) => setDebug(e.target.checked)} /> engine notes
            </label>
          </div>
        </div>
        {view.not_in_log > 0 && (
          <p className="small muted">
            {view.not_in_log} played message{view.not_in_log === 1 ? ' is' : 's are'} not in the log yet.{' '}
            <button className="link" disabled={!!busy} onClick={() => void act('adding to the log', () => api.playSync())}>
              add them
            </button>
          </p>
        )}
        {note && <p className="small muted">{note}</p>}
        {debug && debugLines.length > 0 && (
          <pre className="play-debug">{debugLines.join('\n')}</pre>
        )}
        {error && <p className="error small">{error}</p>}
      </div>
    </div>
  )
}
