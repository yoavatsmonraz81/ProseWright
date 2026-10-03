/** The Spine tab: the authored plan, not a summary of what got written.
 *
 * Every authored beat appears, including the ones with no message range, because
 * "planned and unwritten" is information. If this pane listed the derived beats
 * instead it would quietly redefine the plan as whatever the log happens to
 * contain, and a checkmark would stop meaning anything.
 */

import { useState } from 'react'
import type { AuthoredBeat, DerivedBeat, ManuscriptView, SpineView } from '../types'

const GLYPH: Record<string, string> = {
  written: '\u2713',
  partial: '\u25d0',
  planned: '\u25cb',
  unknown: '\u00b7',
  stale: '\u2298',
  derived: '\u25cf',
}

const EXPLAIN: Record<string, string> = {
  written: 'realised in the log',
  partial: 'aligned, but the audit flagged drift',
  planned: 'authored, not yet written',
  unknown: 'run validate to align authored against written',
  stale: 'aligned to beats the committed spine no longer has — re-derive it',
  derived: 'written prose no authored beat claims',
}

// Read in order, this is the pane's whole grammar: what the mark means, what the
// right-hand column means, and what a click does.
const LEGEND: [string, string][] = [
  ['written', 'the log realises this beat'],
  ['partial', 'realised, but the audit found the prose drifted from the plan'],
  ['planned', 'authored and not yet written — the normal state of a future beat'],
  ['stale', 'the audit points at derived beats the committed spine lacks'],
  ['unknown', 'no audit has run yet; press validate'],
  ['derived', 'a written beat no authored beat claims'],
]

interface Props {
  spine: SpineView | null
  novel: ManuscriptView | null
  showWordCounts: boolean
  /** Unsaved counts from the open manuscript editor, keyed by manuscript scene id. */
  draftWordCounts: Record<string, number> | null
  activeBeat: number | null
  onPickBeat: (beat: DerivedBeat) => void
  onValidate: () => void
  onDerive: () => void
  onCommit: () => void
  onDiscard: () => void
  /** Which long spine job is running, if any. */
  busy: 'validate' | 'derive' | null
  busyPhase?: string
  busyStep?: number
  busyTotal?: number
  error: string | null
}

export function SpineTab({
  spine,
  novel,
  showWordCounts,
  draftWordCounts,
  activeBeat,
  onPickBeat,
  onValidate,
  onDerive,
  onCommit,
  onDiscard,
  busy,
  busyPhase = '',
  busyStep = 0,
  busyTotal = 0,
  error,
}: Props) {
  const [open, setOpen] = useState<string | null>(null)
  const [legend, setLegend] = useState(false)

  if (!spine) return <p className="muted pad">reading the spine…</p>

  const progress =
    busy && busyTotal > 0
      ? Math.min(100, Math.round((busyStep / busyTotal) * 100))
      : null
  const busyLabel =
    busy === 'derive'
      ? 'deriving…'
      : busy === 'validate'
        ? 'aligning…'
        : ''

  const wordsFor = (episode: DerivedBeat): number =>
    (novel?.scenes ?? [])
      .filter((scene) =>
        episode.start_msg_id <= scene.start && scene.start <= episode.end_msg_id,
      )
      .reduce(
        (total, scene) => total + (draftWordCounts?.[scene.id] ?? scene.words),
        0,
      )

  if (spine.unit_type === 'episode' && spine.authority === 'canon_locked') {
    return (
      <div className="rail-body">
        <div className="rail-head">
          <span className="counts">
            {spine.derived.length} canon episodes · locked
          </span>
        </div>
        <p className="stamp-note">
          Titles, identities, and broad envelopes are director-approved. Exact
          message and paragraph seams remain refinable during seam reconciliation.
        </p>
        {error && <p className="error">{error}</p>}
        <ol className="beats loose">
          {spine.derived.map((episode) => (
            <li key={episode.beat_id}>
              <button
                className={`beat-line ${activeBeat === episode.beat_id ? 'is-active' : ''}`}
                onClick={() => onPickBeat(episode)}
              >
                <span className="beat-glyph" aria-hidden>{'\u25cf'}</span>
                <span className="beat-title">
                  <span className="beat-num">
                    E{String(episode.beat_id + 1).padStart(2, '0')}
                  </span>{' '}
                  {episode.title}
                </span>
                <span className="beat-span">
                  {episode.span_label}
                  {showWordCounts && (
                    <span
                      className="beat-words"
                      title="current manuscript word count; updates while editing"
                    >
                      {' · '}{wordsFor(episode).toLocaleString()}w
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ol>
      </div>
    )
  }

  const derivedById = new Map(spine.derived.map((b) => [b.beat_id, b]))
  const unassigned = spine.derived.filter(
    (b) => !spine.authored.some((a) => a.derived_beats.includes(b.beat_id)),
  )
  const pending = spine.pending ?? null

  return (
    <div className="rail-body">
      <div className="rail-head">
        <span className="counts">
          {spine.counts.written} written · {spine.counts.partial} drifted ·{' '}
          {spine.counts.planned} planned
          {spine.counts.stale > 0 && <> · {spine.counts.stale} stale</>}
        </span>
        <button
          className="btn ghost"
          onClick={() => setLegend((v) => !v)}
          aria-expanded={legend}
          title="what the marks mean"
        >
          {legend ? 'hide key' : 'key'}
        </button>
        <button
          className="btn ghost"
          onClick={onDerive}
          disabled={busy !== null}
          title="propose a fresh derived spine from the log (then commit)"
        >
          {busy === 'derive' ? 'deriving…' : 'derive'}
        </button>
        <button
          className="btn"
          onClick={onValidate}
          disabled={busy !== null || !spine.has_derived_spine}
          title={
            spine.has_derived_spine
              ? 'align authored beats against the committed derived spine'
              : 'commit a derived spine first'
          }
        >
          {busy === 'validate' ? 'aligning…' : 'validate'}
        </button>
      </div>

      {busy && (
        <div className="validate-progress" aria-live="polite">
          <div className="validate-progress-track">
            <div
              className="validate-progress-fill"
              style={{
                width:
                  progress === null
                    ? '15%'
                    : `${Math.max(progress, 4)}%`,
              }}
              data-indeterminate={progress === null ? 'true' : 'false'}
            />
          </div>
          <p className="stamp-note validate-progress-label">
            {busyPhase || busyLabel}
            {busyTotal > 0 ? ` · ${busyStep}/${busyTotal}` : ''}
          </p>
        </div>
      )}

      {legend && (
        <dl className="legend">
          {LEGEND.map(([status, meaning]) => (
            <div key={status} className={`legend-row ${status}`}>
              <dt>
                <span className="beat-glyph">{GLYPH[status]}</span>
                {status}
              </dt>
              <dd>{meaning}</dd>
            </div>
          ))}
          <div className="legend-row">
            <dt>
              <span className="beat-glyph">{'\u2014'}</span>
              right column
            </dt>
            <dd>
              the messages a beat occupies, or a dash when nothing in the log
              realises it yet
            </dd>
          </div>
          <div className="legend-row">
            <dt>
              <span className="beat-glyph">{'\u203a'}</span>
              clicking
            </dt>
            <dd>
              a beat opens its authored text; the span beneath it turns the page to
              that stretch of log
            </dd>
          </div>
        </dl>
      )}

      {pending && (
        <div className="pending-spine">
          <h4 className="rail-sub">pending derive</h4>
          <p className="stamp-note">
            {pending.n_beats} beats proposed
            {pending.coverage_ok === false && ' · coverage issues'}
            {pending.coverage_ok === true && ' · coverage ok'}
            — commit to replace the working derived spine, or discard
          </p>
          <ul className="beats loose pending-beats">
            {pending.beats.map((b) => (
              <li key={b.beat_id}>
                <span className="beat-line is-static">
                  <span className="beat-glyph">{'\u25cf'}</span>
                  <span className="beat-title">
                    B{b.beat_id} {b.title}
                  </span>
                  <span className="beat-span">{b.span_label}</span>
                </span>
              </li>
            ))}
          </ul>
          {pending.critique_notes.length > 0 && (
            <ul className="drift">
              {pending.critique_notes.slice(0, 4).map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          )}
          <div className="rail-head pending-actions">
            <button
              className="btn"
              onClick={onCommit}
              disabled={busy !== null}
            >
              commit
            </button>
            <button
              className="btn ghost"
              onClick={onDiscard}
              disabled={busy !== null}
            >
              discard
            </button>
          </div>
        </div>
      )}

      {spine.alignment?.checked && (
        <p className="stamp-note">
          aligned {new Date(spine.alignment.checked).toLocaleString()}
        </p>
      )}
      {!spine.alignment && (
        <p className="stamp-note">
          no alignment yet — statuses stay blank until <em>validate</em> runs
        </p>
      )}
      {spine.tail?.unmapped > 0 && (
        <p className="tail-note">
          The committed spine stops at msg {spine.tail.last_mapped} and the log runs
          to {spine.tail.log_end}, so the last {spine.tail.unmapped} messages belong
          to no beat. Press <em>derive</em>, then <em>commit</em>, to map that
          stretch.
        </p>
      )}
      {error && <p className="error">{error}</p>}

      <ol className="beats">
        {spine.authored.map((beat) => (
          <BeatRow
            key={beat.label}
            beat={beat}
            derived={beat.derived_beats
              .map((id) => derivedById.get(id))
              .filter((b): b is DerivedBeat => Boolean(b))}
            expanded={open === beat.label}
            activeBeat={activeBeat}
            onToggle={() => setOpen(open === beat.label ? null : beat.label)}
            onPickBeat={onPickBeat}
          />
        ))}
      </ol>

      {unassigned.length > 0 && (
        <>
          <h4 className="rail-sub">written, unassigned</h4>
          <p className="stamp-note">
            derived beats no authored beat claims — new prose, or a spine that
            needs re-deriving
          </p>
          <ul className="beats loose">
            {unassigned.map((b) => (
              <li key={b.beat_id}>
                <button
                  className={`beat-line ${activeBeat === b.beat_id ? 'is-active' : ''}`}
                  onClick={() => onPickBeat(b)}
                >
                  <span className="beat-glyph">{'\u25cf'}</span>
                  <span className="beat-title">{b.title}</span>
                  <span className="beat-span">{b.span_label}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {spine.drift.length > 0 && (
        <>
          <h4 className="rail-sub">drift</h4>
          <ul className="drift">
            {spine.drift.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function BeatRow({
  beat,
  derived,
  expanded,
  activeBeat,
  onToggle,
  onPickBeat,
}: {
  beat: AuthoredBeat
  derived: DerivedBeat[]
  expanded: boolean
  activeBeat: number | null
  onToggle: () => void
  onPickBeat: (beat: DerivedBeat) => void
}) {
  const span =
    beat.start != null && beat.end != null
      ? `${beat.start}–${beat.end}`
      : '—'
  const active = derived.some((d) => d.beat_id === activeBeat)

  return (
    <li className={`beat ${beat.status} ${active ? 'is-active' : ''}`}>
      <button
        className="beat-line"
        onClick={onToggle}
        aria-expanded={expanded}
        title={EXPLAIN[beat.status] ?? beat.status}
      >
        <span className="beat-glyph" aria-hidden>
          {GLYPH[beat.status] ?? '\u00b7'}
        </span>
        <span className="beat-title">
          <span className="beat-num">{beat.label}</span> {beat.title}
        </span>
        <span className="beat-span">{span}</span>
      </button>
      {expanded && (
        <div className="beat-body">
          <p className="beat-text">{beat.text}</p>
          {beat.verdict_note && (
            <p className="stamp-note">{beat.verdict_note}</p>
          )}
          {beat.status === 'stale' && beat.missing_derived.length > 0 && (
            <p className="tail-note">
              The last audit named derived beat
              {beat.missing_derived.length === 1 ? '' : 's'}{' '}
              {beat.missing_derived.join(', ')}, which the committed spine does not
              contain — it was derived before this prose existed. Re-derive the spine
              (derive → commit), then validate again.
            </p>
          )}
          {derived.length > 0 && (
            <ul className="beat-links">
              {derived.map((d) => (
                <li key={d.beat_id}>
                  <button
                    className="linkish"
                    onClick={() => onPickBeat(d)}
                  >
                    B{d.beat_id} {d.title} · {d.span_label}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  )
}
