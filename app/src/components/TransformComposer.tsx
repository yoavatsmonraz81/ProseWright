/** The composer: a plain-language note, and whatever else the verb needs.
 *
 * Every operator here proposes and writes nothing, so this sheet ends at
 * "propose" — the verdict happens in the fork pane, against a diff. That split
 * is the whole discipline of the transform half, and the copy says so rather
 * than leaving the button to imply it.
 *
 * The verb chips are handed in, already filtered by what the engine says runs on
 * the visible layer, so a verb this sheet cannot honour is never on it.
 */

import { useEffect, useRef, useState } from 'react'
import { ApiError, api, streamPropose } from '../api'
import type { StampReport, SweepReport, WeedReport, WeedSource } from '../types'
import type { Verb } from '../lib/verbs'

interface Props {
  verbs: Verb[]
  verb: string
  range: { from: number; to: number } | null
  /** The scene on the page — weed can walk this without a selection. */
  sceneRange: { from: number; to: number } | null
  /** Who speaks inside the selection — the filter for restyle, the cast for inject. */
  speakers: string[]
  onVerb: (verb: string) => void
  onClose: () => void
  onProposed: () => void
  onSwept: (report: SweepReport) => void
  onWeeded: (report: WeedReport) => void
  onStamped: (report: StampReport) => void
}

const NEEDS_NOTE: Record<string, boolean> = { restyle: true, inject: true }


function elapsedLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`
}

export function TransformComposer({
  verbs,
  verb,
  range,
  sceneRange,
  speakers,
  onVerb,
  onClose,
  onProposed,
  onSwept,
  onWeeded,
  onStamped,
}: Props) {
  const [note, setNote] = useState('')
  const [mode, setMode] = useState<'tighten' | 'expand'>('tighten')
  const [who, setWho] = useState('')
  const [voice, setVoice] = useState(speakers[0] ?? '')
  const [weedScope, setWeedScope] = useState<'selection' | 'scene' | 'log'>(
    range ? 'selection' : sceneRange ? 'scene' : 'log',
  )
  const [weedSources, setWeedSources] = useState<WeedSource[]>([])
  const [selectedWeedSources, setSelectedWeedSources] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [phase, setPhase] = useState('')
  const [chars, setChars] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  useEffect(() => {
    if (!running) {
      if (timer.current) window.clearInterval(timer.current)
      return
    }
    setElapsed(0)
    timer.current = window.setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [running])

  useEffect(() => {
    if (verb !== 'weed') return
    void api.weedSources().then((result) => {
      setWeedSources(result.sources)
      setSelectedWeedSources(result.selected_sources)
    }).catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [verb])

  const chosen = verbs.find((v) => v.name === verb) ?? null
  const span = range
    ? range.from === range.to
      ? `msg ${range.from}`
      : `msgs ${range.from}\u2013${range.to}`
    : 'no selection'
  const at = range?.to ?? null

  const weedRange = (): { from?: number; to?: number } | undefined => {
    if (weedScope === 'selection') return range ?? undefined
    if (weedScope === 'scene') return sceneRange ?? undefined
    return undefined
  }

  const blocked =
    running ||
    !chosen ||
    chosen.state !== 'ready' ||
    (verb === 'inject' && !voice.trim()) ||
    (verb === 'weed' && selectedWeedSources.length === 0) ||
    ((verb === 'weed' || verb === 'copyedit' || verb === 'stamps') &&
      weedScope === 'selection' &&
      !range) ||
    ((verb === 'weed' || verb === 'copyedit' || verb === 'stamps') &&
      weedScope === 'scene' &&
      !sceneRange) ||
    (NEEDS_NOTE[verb] && !note.trim())

  const run = async (action: 'propose' | 'scan' = 'propose') => {
    setRunning(true)
    setError(null)
    setPhase('')
    setChars(0)
    try {
      if (verb === 'sweep') {
        const result = await api.sweepLast()
        onSwept(result.report)
        return
      }
      const onEvent = (event: { kind: string; text?: string }) => {
        if (event.kind === 'phase') setPhase(event.text ?? '')
        else if (event.kind === 'content') setChars((c) => c + (event.text?.length ?? 0))
      }
      if (verb === 'restyle') {
        await streamPropose(
          '/restyle',
          {
            note: note.trim(),
            from: range?.from,
            to: range?.to,
            speaker: who || undefined,
          },
          onEvent,
        )
      } else if (verb === 'retune') {
        await streamPropose(
          '/retune',
          {
            mode,
            note: note.trim() || undefined,
            from: range?.from,
            to: range?.to,
            speaker: who || undefined,
          },
          onEvent,
        )
      } else if (verb === 'inject') {
        await streamPropose(
          '/inject',
          { note: note.trim(), after: at, speaker: voice.trim() },
          onEvent,
        )
      } else if (verb === 'weed') {
        const scope = weedRange()
        if (action === 'scan') {
          const report = await api.scanWeed({
            from: scope?.from,
            to: scope?.to,
            speaker: who || undefined,
            sources: selectedWeedSources,
          })
          onWeeded(report)
          return
        }
        await streamPropose(
          '/weed',
          {
            from: scope?.from,
            to: scope?.to,
            speaker: who || undefined,
            sources: selectedWeedSources,
          },
          onEvent,
        )
      } else if (verb === 'copyedit') {
        const scope = weedRange()
        await streamPropose(
          '/copyedit',
          {
            from: scope?.from,
            to: scope?.to,
            speaker: who || undefined,
          },
          onEvent,
        )
      } else if (verb === 'stamps') {
        const scope = weedRange()
        if (action === 'scan') {
          const report = await api.scanStamps({
            from: scope?.from,
            to: scope?.to,
          })
          onStamped(report)
          return
        }
        await streamPropose(
          '/stamps',
          {
            from: scope?.from,
            to: scope?.to,
          },
          onEvent,
        )
      }
      onProposed()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div
      className="sheet-scrim"
      onClick={() => {
        if (!running) onClose()
      }}
    >
      <div
        className="sheet composer"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !running) onClose()
        }}
      >
        <header>
          <h3>Propose a change</h3>
          <button className="chip ghost" disabled={running} onClick={onClose}>
            {running ? 'working' : 'close'}
          </button>
        </header>

        <div className="row wrap verb-chips">
          {verbs.map((v) => (
            <button
              key={v.name}
              className={`chip ${v.name === verb ? 'is-on' : ''}`}
              disabled={running || v.state !== 'ready'}
              title={v.reason || v.what}
              onClick={() => onVerb(v.name)}
            >
              {v.name}
            </button>
          ))}
        </div>
        <p className="hint">
          {chosen
            ? chosen.state === 'ready'
              ? chosen.what
              : chosen.reason
            : 'No verb runs on this layer yet.'}
        </p>

        {running ? (
          <div className="composer-working">
            <p className="stamp-note">working</p>
            <p>{phase || `${verb}\u2026`}</p>
            <p className="muted small">
              {elapsedLabel(elapsed)} elapsed
              {chars > 0 ? ` \u00b7 ${chars.toLocaleString()} characters written` : ''}. A
              local model takes a minute or two per turn; nothing is written to the log
              either way.
            </p>
          </div>
        ) : (
          <>
            {verb !== 'sweep' && verb !== 'weed' && verb !== 'copyedit' && verb !== 'stamps' && (
              <p className="composer-span">
                {verb === 'inject'
                  ? at === null
                    ? 'Select a turn to insert after.'
                    : `inserted after msg ${at}`
                  : span}
                {speakers.length > 0 && (
                  <span className="muted"> · {speakers.join(', ')}</span>
                )}
              </p>
            )}

            {(verb === 'weed' || verb === 'copyedit' || verb === 'stamps') && (
              <Field label="how far">
                <div className="row wrap">
                  <button
                    className={`chip ${weedScope === 'selection' ? 'is-on' : ''}`}
                    disabled={!range}
                    onClick={() => setWeedScope('selection')}
                  >
                    this selection
                  </button>
                  <button
                    className={`chip ${weedScope === 'scene' ? 'is-on' : ''}`}
                    disabled={!sceneRange}
                    onClick={() => setWeedScope('scene')}
                  >
                    this scene
                  </button>
                  <button
                    className={`chip ${weedScope === 'log' ? 'is-on' : ''}`}
                    onClick={() => setWeedScope('log')}
                  >
                    the whole log
                  </button>
                </div>
                <p className="hint">
                  {verb === 'weed'
                    ? 'Finds watchlist phrases — load-bearing and kin — only in the authorship sources you select. A match is not proof of AI authorship. You accept or reject each proposed sentence in the fork pane.'
                    : verb === 'copyedit'
                      ? 'Chunked typo pass. The model may only name a short exact substring and its correction. Headers stay put. You accept each turn in the fork pane.'
                      : 'Walks the log and carries the last date, time, and location forward. Scan writes metadata; propose prepends missing CHAR headers for you to accept.'}
                </p>
              </Field>
            )}

            {verb === 'weed' && weedSources.length > 0 && (
              <Field label="suspected AI sources">
                <div className="row wrap">
                  {weedSources.map((source) => {
                    const selected = selectedWeedSources.includes(source.id)
                    return (
                      <button
                        key={source.id}
                        className={`chip ${selected ? 'is-on' : ''}`}
                        onClick={() => {
                          const next = selected
                            ? selectedWeedSources.filter((id) => id !== source.id)
                            : [...selectedWeedSources, source.id]
                          setSelectedWeedSources(next)
                          void api.setWeedSources(next).catch((err) =>
                            setError(err instanceof ApiError ? err.message : String(err)),
                          )
                        }}
                      >
                        {source.label} · {source.message_count}
                      </button>
                    )
                  })}
                </div>
                <p className="hint">
                  Authorship source, not narrative character. Human-written turns
                  stay excluded unless you explicitly select them.
                </p>
              </Field>
            )}

            {(verb === 'restyle' || verb === 'retune' || verb === 'weed' || verb === 'copyedit') &&
              speakers.length > 1 && (
              <Field label="whose lines">
                <div className="row wrap">
                  <button
                    className={`chip ${who === '' ? 'is-on' : ''}`}
                    onClick={() => setWho('')}
                  >
                    everyone
                  </button>
                  {speakers.map((s) => (
                    <button
                      key={s}
                      className={`chip ${who === s ? 'is-on' : ''}`}
                      onClick={() => setWho(s)}
                    >
                      {s}
                    </button>
                  ))}
                </div>
                <p className="hint">
                  A turn left out of the filter is not sent to the model at all, so
                  another character’s lines cannot drift.
                </p>
              </Field>
            )}

            {verb === 'retune' && (
              <Field label="direction">
                <div className="row">
                  {(['tighten', 'expand'] as const).map((m) => (
                    <button
                      key={m}
                      className={`chip ${mode === m ? 'is-on' : ''}`}
                      onClick={() => setMode(m)}
                    >
                      {m}
                    </button>
                  ))}
                </div>
                <p className="hint">
                  Length and density only. The register, the events and the facts stay
                  exactly as they are.
                </p>
              </Field>
            )}

            {verb === 'inject' && (
              <Field label="as">
                <div className="row wrap">
                  {speakers.map((s) => (
                    <button
                      key={s}
                      className={`chip ${voice === s ? 'is-on' : ''}`}
                      onClick={() => setVoice(s)}
                    >
                      {s}
                    </button>
                  ))}
                </div>
                <div className="row">
                  <input
                    value={voice}
                    placeholder="who writes the new turn"
                    onChange={(e) => setVoice(e.target.value)}
                  />
                </div>
              </Field>
            )}

            {verb === 'sweep' && (
              <p className="hint">
                Sweep changes nothing. It reads the last committed change, looks for
                later turns that now contradict it, and reports what it finds.
              </p>
            )}

            {verb !== 'sweep' && verb !== 'weed' && verb !== 'copyedit' && verb !== 'stamps' && (
              <Field
                label={NEEDS_NOTE[verb] ? 'the change to make' : 'note (optional)'}
              >
                <textarea
                  autoFocus
                  rows={4}
                  value={note}
                  placeholder={placeholderFor(verb)}
                  onChange={(e) => setNote(e.target.value)}
                />
              </Field>
            )}

            {error && <p className="error">{error}</p>}

            {verb === 'weed' || verb === 'stamps' ? (
              <div className="row wrap">
                <button
                  className="stamp-button"
                  disabled={blocked}
                  onClick={() => void run('scan')}
                >
                  {verb === 'stamps' ? 'scan the clock' : 'find them'}
                </button>
                <button
                  className="stamp-button"
                  disabled={blocked}
                  onClick={() => void run('propose')}
                >
                  {verb === 'stamps' ? 'propose headers' : 'propose pulls'}
                </button>
              </div>
            ) : (
              <button className="stamp-button" disabled={blocked} onClick={() => void run()}>
                {verb === 'sweep' ? 'look downstream' : 'propose'}
              </button>
            )}
            <p className="muted small">
              Nothing is written yet. The proposal opens in the fork pane as a diff,
              and only a verdict there touches the log.
            </p>
          </>
        )}
      </div>
    </div>
  )
}

function placeholderFor(verb: string): string {
  if (verb === 'restyle') {
    return 'e.g. colder, more withheld — she is deciding whether to trust him and the prose should not settle it'
  }
  if (verb === 'inject') return 'what the new turn does, and no more than that'
  if (verb === 'retune') return 'anything to sharpen the pass (optional)'
  return 'a direction (optional)'
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="field">
      <h4>{label}</h4>
      {children}
    </section>
  )
}
