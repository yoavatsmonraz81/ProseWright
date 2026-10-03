/** The novel layer of the page.
 *
 * Two states, and which one you see is simply whether this scene has been
 * novelized. Before: what a pass would do, in the voice you choose. After: the
 * prose, with the verdict buttons that decide whether it reaches an export.
 *
 * The prose is a plain paper panel rather than the log's CodeMirror page — the
 * manuscript has no message ids to number and no gutter marks;
 * it is meant to read like a book, which is the whole point of the layer.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import type { DerivedScene, NovelizePlan, ProseReviewProposal, SceneRow, Voice } from '../types'
import type { PageType } from '../lib/typography'
import { VoicePicker } from './VoicePicker'
import { wordDiff, type Range } from '../lib/wordDiff'
import { withEmDash } from '../lib/emDash'

interface Props {
  scene: SceneRow
  type: PageType
  onNovelized: () => void
  onReviewChanged: () => void
  onDraftWordCounts: (counts: Record<string, number> | null) => void
  reviewRevision?: number
}

const STATUS_WORD: Record<string, string> = {
  draft: 'awaiting your verdict',
  approved: 'approved for export',
  rejected: 'rejected',
}

// Interior thought arrives from the log as *italics*, and a manuscript that
// prints its own asterisks is not a manuscript. Deliberately just emphasis:
// anything more would be a markdown renderer, and the prose is not markdown.
const EMPHASIS = /\*\*([^*]+)\*\*|\*([^*\n]+)\*/g

function renderEmphasis(text: string, marks: Range[] = [], markClass = ''): ReactNode[] {
  // `marks` underline the stretches a proposal touches; they are split around
  // the emphasis markers so an edit inside *italics* stays italic and marked.
  const pieces = (start: number, end: number, key: string): ReactNode[] => {
    const out: ReactNode[] = []
    let at = start
    for (const [from, to] of marks) {
      if (to <= at || from >= end) continue
      const s = Math.max(from, at)
      const e = Math.min(to, end)
      if (s > at) out.push(text.slice(at, s))
      out.push(<span className={markClass} key={`${key}-${s}`}>{text.slice(s, e)}</span>)
      at = e
    }
    if (at < end) out.push(text.slice(at, end))
    return out
  }
  const out: ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(EMPHASIS)) {
    const at = match.index ?? 0
    if (at > last) out.push(...pieces(last, at, `t${last}`))
    const fence = match[1] ? 2 : 1
    const inner = pieces(at + fence, at + match[0].length - fence, `e${at}`)
    if (match[1]) out.push(<strong key={at}>{inner}</strong>)
    else out.push(<em key={at}>{inner}</em>)
    last = at + match[0].length
  }
  if (last < text.length) out.push(...pieces(last, text.length, `t${last}`))
  return out
}

function minutes(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`
}

function wordCount(text: string): number {
  const prose = text.trim()
  return prose ? prose.split(/\s+/).length : 0
}

export function NovelPane({ scene, type, onNovelized, onReviewChanged, onDraftWordCounts, reviewRevision = 0 }: Props) {
  const [plan, setPlan] = useState<NovelizePlan | null>(null)
  const [derivedParts, setDerivedParts] = useState<DerivedScene[]>([])
  const [voice, setVoice] = useState<Partial<Voice>>({})
  const [direction, setDirection] = useState('')
  const [showDirection, setShowDirection] = useState(false)
  const [drafts, setDrafts] = useState<Record<string, string> | null>(null)
  const [proseProposals, setProseProposals] = useState<ProseReviewProposal[]>([])
  const [reviewFilter, setReviewFilter] = useState<'pending' | 'verify' | 'committed' | 'stale' | 'rejected' | 'all'>('pending')
  const [proposalDrafts, setProposalDrafts] = useState<Record<string, Record<string, string>>>({})
  const [warnings, setWarnings] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [rhythmRunning, setRhythmRunning] = useState(false)
  const [phase, setPhase] = useState('')
  const [step, setStep] = useState(0)
  const [total, setTotal] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** Committed one-pass ceiling; null until the first plan lands. */
  const [spanCeiling, setSpanCeiling] = useState<number | null>(null)
  const [spanDraft, setSpanDraft] = useState('')
  const timer = useRef<number | null>(null)

  // Reset per scene: a voice chosen for one scene is not a decision about the next.
  useEffect(() => {
    setVoice({})
    setDirection('')
    setShowDirection(false)
    setDrafts(null)
    setProseProposals([])
    setReviewFilter('pending')
    setProposalDrafts({})
    setWarnings([])
    setError(null)
    setPhase('')
    setStep(0)
    setTotal(0)
    setSpanCeiling(null)
    setSpanDraft('')
    onDraftWordCounts(null)
    return () => onDraftWordCounts(null)
  }, [scene.scene_id, onDraftWordCounts])

  const load = useCallback(async () => {
    try {
      const front = scene.kind === 'frontmatter' || scene.start < 0
      const refs = scene.manuscript_parts?.length
        ? scene.manuscript_parts
        : scene.manuscript
          ? [{ ...scene.manuscript, start: scene.start, end: scene.end }]
          : []
      const [p, parts, reviews] = await Promise.all([
        front
          ? Promise.resolve(null)
          : api.novelizePlan(
              scene.start,
              scene.end,
              {},
              spanCeiling != null ? { max_span_chars: spanCeiling } : {},
            ),
        Promise.all(refs.map((part) => api.scene(part.id))),
        Promise.all(refs.map((part) => api.proseReview(part.id, reviewFilter === 'verify' ? 'pending' : reviewFilter))),
      ])
      setPlan(p)
      setDerivedParts(parts.sort((a, b) => a.source.start - b.source.start))
      const proposals = reviews.flatMap((result) => result.proposals)
      setProseProposals(reviewFilter === 'verify'
        ? proposals.filter((proposal) => proposal.reason.toUpperCase().includes('VERIFY'))
        : proposals)
      if (!front && p && spanCeiling == null) {
        setSpanCeiling(p.max_span_chars)
        setSpanDraft(String(p.max_span_chars))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [scene.start, scene.end, scene.kind, scene.manuscript, scene.manuscript_parts, spanCeiling, reviewFilter])

  useEffect(() => {
    void load()
  }, [load, reviewRevision])

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

  const commitSpanCeiling = () => {
    const n = Number.parseInt(spanDraft.replace(/,/g, ''), 10)
    if (!Number.isFinite(n) || n < 500) {
      setError('chars per pass must be an integer ≥ 500')
      if (spanCeiling != null) setSpanDraft(String(spanCeiling))
      return
    }
    setError(null)
    if (n !== spanCeiling) setSpanCeiling(n)
    setSpanDraft(String(n))
  }

  const run = async (regenerate: boolean) => {
    setRunning(true)
    setPhase('starting…')
    setStep(0)
    setTotal(plan?.chunk_count && plan.chunk_count > 0 ? plan.chunk_count : 0)
    setError(null)
    setWarnings([])
    try {
      const result = await api.streamNovelize(
        scene.start,
        scene.end,
        {
          ...voice,
          regenerate,
          direction: direction.trim() || undefined,
          max_span_chars: spanCeiling ?? undefined,
        },
        (event) => {
          if (event.kind !== 'phase') return
          if (event.text) setPhase(event.text)
          if (typeof event.total === 'number' && event.total > 0) {
            setTotal(event.total)
          }
          if (typeof event.step === 'number' && event.step >= 0) {
            setStep(event.step)
          }
        },
      )
      setWarnings(result.warnings)
      setDerivedParts(result.scene ? [result.scene] : [])
      setDirection('')
      setShowDirection(false)
      onNovelized()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setRunning(false)
      setPhase('')
      setStep(0)
      setTotal(0)
    }
  }

  const verdict = async (status: 'approved' | 'rejected' | 'draft') => {
    if (!derivedParts.length) return
    setBusy(true)
    try {
      setDerivedParts(await Promise.all(
        derivedParts.map((part) => api.setSceneStatus(part.id, status)),
      ))
      onNovelized()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    if (!derivedParts.length || drafts === null) return
    setBusy(true)
    try {
      setDerivedParts(await Promise.all(
        derivedParts.map((part) => api.editScene(part.id, drafts[part.id] ?? part.text)),
      ))
      setDrafts(null)
      onDraftWordCounts(null)
      onNovelized()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const smoothRhythm = async () => {
    setBusy(true)
    setRhythmRunning(true)
    setRunning(true)
    setStep(0)
    setTotal(0)
    setPhase('reviewing phrasing and punctuation — manuscript only…')
    setError(null)
    setWarnings([])
    try {
      const notes: string[] = []
      let count = 0
      for (const part of derivedParts) {
        const result = await api.streamRhythm(part.id, (event) => {
          if (event.kind !== 'phase') return
          if (event.text) setPhase(event.text)
          if (typeof event.step === 'number') setStep(event.step)
          if (typeof event.total === 'number') setTotal(event.total)
        })
        count += result.count
        notes.push(...result.warnings)
      }
      setWarnings([...notes, count ? `${count} phrasing proposals queued below. Nothing committed.` : 'No new phrasing proposals; existing or rejected suggestions are not duplicated.'])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      await load()
      setBusy(false)
      setRunning(false)
      setRhythmRunning(false)
      setPhase('')
    }
  }

  const rebase = async () => {
    if (!derivedParts.length) return
    setBusy(true)
    try {
      setDerivedParts(await Promise.all(
        derivedParts.map((part) => api.rebaseScene(part.id)),
      ))
      onNovelized()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const pin = async () => {
    if (!derivedParts.length) return
    setBusy(true)
    try {
      setDerivedParts(await Promise.all(
        derivedParts.map((part) => api.pinScene(part.id)),
      ))
      onNovelized()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const decideProseProposal = async (
    proposal: ProseReviewProposal,
    action: 'commit' | 'reject',
  ) => {
    setBusy(true)
    setError(null)
    try {
      if (action === 'commit') await api.commitProseProposal(proposal.id, proposalDrafts[proposal.id])
      else await api.rejectProseProposal(proposal.id)
      await load()
      onNovelized()
      onReviewChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      // A stale commit changes the queue even though it answers 409.
      await load().catch(() => undefined)
      onReviewChanged()
    } finally {
      setBusy(false)
    }
  }

  // The measure is the text's width; the paper is that plus its margins, which
  // is how the log page is sized too, so switching layers does not resize the page.
  const prose = {
    fontFamily: `"${type.family}", var(--font-mono)`,
    fontSize: `${type.size}px`,
    lineHeight: type.lineHeight,
    maxWidth: `calc(${type.measure}ch + 12ch)`,
    letterSpacing: `${type.tracking}em`,
  }
  const derived = derivedParts[0] ?? null

  if (running) {
    const passes = !rhythmRunning && plan?.chunk_count && plan.chunk_count > 1 ? plan.chunk_count : 0
    const plannedTotal = total > 0 ? total : passes || 0
    const progressPct =
      plannedTotal > 0
        ? Math.min(100, Math.round((Math.max(step, 0) / plannedTotal) * 100))
        : null
    return (
      <div className="page novel working">
        <p className="stamp">{rhythmRunning ? 'smoothing rhythm' : 'novelizing'}</p>
        <p>
          {plan ? `${plan.turns} turns` : 'the scene'} in{' '}
          {plan?.voice_label ?? 'the book’s voice'}
          {passes ? ` · ${passes} passes` : ''}.
        </p>
        <div className="novel-progress" aria-live="polite">
          <div className="novel-progress-track">
            <div
              className="novel-progress-fill"
              style={{
                width:
                  progressPct === null
                    ? '18%'
                    : `${Math.max(progressPct, 4)}%`,
              }}
              data-indeterminate={progressPct === null ? 'true' : 'false'}
            />
          </div>
          <p className="muted novel-progress-label">
            {minutes(elapsed)} elapsed
            {plannedTotal > 0 ? ` · pass ${Math.min(step || 1, plannedTotal)}/${plannedTotal}` : ''}
            {phase ? ` — ${phase}` : ' — waiting on the model'}
          </p>
        </div>
        <p className="muted small">
          {rhythmRunning ? 'Changes will appear as proposals below the prose; nothing is committed automatically.' : 'The page will fill itself when the pass lands.'} Local models often take
          several minutes per scene.
        </p>
      </div>
    )
  }

  // ------------------------------------------------------------------ before
  if (!derived) {
    if (scene.kind === 'frontmatter') {
      return (
        <div className="page novel offer">
          {error && <p className="error">{error}</p>}
          <p className="muted">This front matter has no prose on file.</p>
        </div>
      )
    }
    return (
      <div className="page novel offer">
        {error && <p className="error">{error}</p>}
        <h3>Not yet novelized</h3>
        {plan ? (
          <>
            <p className="muted">
              {plan.turns} turns · {plan.source_chars.toLocaleString()} characters
              {plan.title ? ` · ${plan.title}` : ''}
            </p>
            <VoicePicker
              voice={{ ...plan.voice, ...voice }}
              persons={plan.persons}
              tenses={plan.tenses}
              cast={plan.focal_candidates}
              onChange={(patch) => setVoice((v) => ({ ...v, ...patch }))}
            />
            <p className="muted small">
              The book is written in {formatVoice(plan.book_voice, plan)}. Changing
              it here writes this scene in a different voice and leaves the book’s
              default alone.
            </p>
            <label className="muted small novel-span-ceiling">
              chars per pass{' '}
              <input
                type="text"
                inputMode="numeric"
                value={spanDraft}
                onChange={(e) => setSpanDraft(e.target.value)}
                onBlur={() => commitSpanCeiling()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    ;(e.target as HTMLInputElement).blur()
                  }
                }}
                title="Source characters one model call may hold. Raise for fewer passes if your model can finish larger chunks; lower if outputs truncate."
              />
              {plan.chunk_count > 1
                ? ` · ${plan.chunk_count} passes at this ceiling`
                : ''}
            </label>
            {plan.unchunkable ? (
              <p className="warn">
                This scene has a single turn over the{' '}
                {plan.max_span_chars.toLocaleString()}-character one-pass ceiling
                and cannot be split further. Shorten that message in the log
                before novelizing.
              </p>
            ) : plan.too_long && plan.chunk_count > 1 ? (
              <>
                <p className="muted small">
                  {plan.source_chars.toLocaleString()} characters — over the{' '}
                  {plan.max_span_chars.toLocaleString()} one-pass ceiling, so this
                  runs as {plan.chunk_count} contiguous passes with continuity
                  between them.
                </p>
                <button className="stamp-button" onClick={() => void run(false)}>
                  novelize in {plan.chunk_count} passes
                </button>
              </>
            ) : (
              <button className="stamp-button" onClick={() => void run(false)}>
                novelize this scene
              </button>
            )}
          </>
        ) : (
          <p className="muted">reading the scene…</p>
        )}
      </div>
    )
  }

  // ------------------------------------------------------------------- after
  const statuses = [...new Set(derivedParts.map((part) => part.status))]
  const status = statuses.length === 1 ? statuses[0] : 'mixed'
  const words = derivedParts.reduce((sum, part) => sum + part.words, 0)
  const blocks = derivedParts.reduce((sum, part) => sum + part.blocks.length, 0)
  const driftedParts = derivedParts.filter((part) => part.drift?.kind)
  const drifted = driftedParts.some((part) => part.drift.kind !== 'pinned')
    ? 'dirty'
    : driftedParts.length
      ? 'pinned'
      : ''
  const startEditing = () => {
    const next = Object.fromEntries(derivedParts.map((part) => [part.id, part.text]))
    setDrafts(next)
    onDraftWordCounts(Object.fromEntries(
      derivedParts.map((part) => [part.id, wordCount(part.text)]),
    ))
  }

  const updateDraft = (partId: string, text: string) => {
    const next = { ...(drafts ?? {}), [partId]: text }
    setDrafts(next)
    onDraftWordCounts(Object.fromEntries(
      Object.entries(next).map(([id, value]) => [id, wordCount(value)]),
    ))
  }

  const discardDrafts = () => {
    setDrafts(null)
    onDraftWordCounts(null)
  }
  return (
    <div className="page novel">
      <div className="novel-head">
        <span className={`chip status-${status}`}>
          {status} · {status === 'mixed' ? 'part verdicts differ' : STATUS_WORD[status] ?? ''}
        </span>
        <span className="muted small">
          {derived.voice_label || 'the book’s voice'} · {words.toLocaleString()} words
          {blocks
            ? ` · ${blocks} ${scene.kind === 'frontmatter' ? 'blocks' : 'paragraphs'}`
            : ''}
          {derivedParts.length > 1 ? ` · ${derivedParts.length} manuscript parts` : ''}
        </span>
        <div className="novel-head-actions">
          <button
            className="chip ghost"
            disabled={busy || running || drafts !== null || scene.kind === 'frontmatter'}
            onClick={() => void smoothRhythm()}
            title="Propose repairs for accidental fragments, staccato phrasing, and excessive em-dash chaining. Review before committing; dialogue and source log stay unchanged."
          >
            {busy && running ? 'reviewing phrasing…' : 'repair phrasing'}
          </button>
          {drafts === null ? (
            <button
              className="chip ghost"
              disabled={busy}
              onClick={startEditing}
              title="edit this novelized scene directly"
            >
              edit
            </button>
          ) : (
            <>
              <button className="chip ok" disabled={busy} onClick={() => void save()}>
                save
              </button>
              <button className="chip ghost" disabled={busy} onClick={discardDrafts}>
                discard
              </button>
            </>
          )}
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      {warnings.map((w) => (
        <p className="warn" key={w}>
          {w}
        </p>
      ))}

      {drifted && drifted !== 'pinned' && (
        <p className="warn drift">
          The log under {derivedParts.length > 1 ? 'one or more parts of this scene' : 'this scene'} has changed
          {driftedParts[0]?.drift.detail ? ` (${driftedParts[0].drift.detail})` : ''}.
          The prose still stands as prose, but it is no longer a faithful
          derivation.{' '}
          <button className="link" disabled={busy} onClick={() => void rebase()}>
            rebase
          </button>
          {' · '}
          <button className="link" disabled={busy} onClick={() => void pin()}>
            pin divergence
          </button>
          {' · '}
          or re-roll below.
        </p>
      )}
      {drifted === 'pinned' && (
        <p className="muted small">
          Divergence pinned — export treats this as intentional.{' '}
          <button className="link" disabled={busy} onClick={() => void rebase()}>
            rebase instead
          </button>
        </p>
      )}

      {drafts === null ? (
        <article
          className={`manuscript${derivedParts.some((part) => part.blocks.some((b) => b.kind === 'heading' || b.kind === 'break')) ? ' file' : ''}`}
          style={prose}
        >
          {derivedParts.flatMap((part) => part.blocks.map((b) => {
            const body = renderEmphasis(b.text)
            const cls = [b.edited ? 'edited' : '', b.note || ''].filter(Boolean).join(' ')
            if (b.kind === 'break') {
              return <hr key={b.id} className="ms-rule" />
            }
            if (b.kind === 'heading') {
              const Tag = (b.note === 'h2' ? 'h2' : b.note === 'h4' ? 'h4' : 'h3') as
                | 'h2'
                | 'h3'
                | 'h4'
              return (
                <Tag key={b.id} className={cls} title={sourceHint(b.src)}>
                  {body}
                </Tag>
              )
            }
            return (
              <p key={b.id} className={cls} title={sourceHint(b.src)}>
                {body}
              </p>
            )
          }))}
        </article>
      ) : (
        <div className="manuscript-edit-stack">
          {derivedParts.map((part, index) => (
            <label key={part.id} className="manuscript-edit-part">
              {derivedParts.length > 1 && (
                <span className="manuscript-part-label">
                  part {index + 1} of {derivedParts.length} · messages {part.source.start}–{part.source.end}
                </span>
              )}
              <textarea
                className="manuscript-edit"
                style={prose}
                value={drafts[part.id] ?? ''}
                onChange={(e) => updateDraft(part.id, withEmDash(e.target))}
                spellCheck
              />
            </label>
          ))}
        </div>
      )}

      <section className="prose-review" aria-label="Prose revision proposals">
          <div className="prose-review-head">
            <h3>Prose review</h3>
            <span className="muted small">
              {proseProposals.length} {reviewFilter === 'all' ? 'proposals' : reviewFilter} · manuscript only
            </span>
            <label className="prose-review-filter">
              show{' '}
              <select value={reviewFilter} onChange={(event) => setReviewFilter(event.target.value as typeof reviewFilter)}>
                <option value="pending">pending</option>
                <option value="verify">VERIFY</option>
                <option value="committed">committed</option>
                <option value="stale">stale</option>
                <option value="rejected">rejected</option>
                <option value="all">all</option>
              </select>
            </label>
          </div>
          {proseProposals.length === 0 && <p className="muted small">No proposals in this view.</p>}
          {drafts !== null && (
            <p className="warn small">
              Save or discard your hand edit before committing a proposal.
            </p>
          )}
          {proseProposals.map((proposal) => {
            const diff = wordDiff(proposal.before, proposal.after)
            return (
            <article className={`prose-proposal ${proposal.kind}`} key={proposal.id}>
              <div className="prose-proposal-head">
                {reviewFilter === 'all' && <span className="muted small">{proposal.status}</span>}
                <span className={`chip proposal-${proposal.kind}`}>
                  {proposal.kind === 'syntax' ? 'syntax repair' : proposal.kind === 'rhythm' ? 'rhythm repair' : 'compression'}
                </span>
                <span className="muted small">
                  {proposal.word_delta < 0
                    ? `${Math.abs(proposal.word_delta)} words removed`
                    : proposal.word_delta > 0
                      ? `${proposal.word_delta} words added`
                      : 'same word count'}
                </span>
              </div>
              <p className="prose-proposal-reason">{proposal.reason}</p>
              {proposal.risks.length > 0 && (
                <p className="warn small">Watch: {proposal.risks.join(' · ')}</p>
              )}
              <div className="prose-proposal-diff">
                <div>
                  <span className="prose-proposal-label">before</span>
                  <p>{renderEmphasis(proposal.before, diff.before, 'proposal-changed')}</p>
                </div>
                <div>
                  <span className="prose-proposal-label">proposed</span>
                  {proposalDrafts[proposal.id] ? (
                    <>
                      {proposal.changes.map((change, index) => (
                        <textarea
                          key={change.block_id}
                          className="prose-proposal-editor"
                          aria-label={`Proposed replacement ${index + 1}`}
                          value={proposalDrafts[proposal.id][change.block_id]}
                          disabled={busy}
                          onChange={(event) => setProposalDrafts((current) => ({
                            ...current,
                            [proposal.id]: { ...current[proposal.id], [change.block_id]: withEmDash(event.target) },
                          }))}
                        />
                      ))}
                      <p className="small">Your wording is applied only when you commit. Keep *italics* markers; empty text removes that paragraph.</p>
                    </>
                  ) : <p>{renderEmphasis(proposal.after, diff.after, 'proposal-inserted')}</p>}
                </div>
              </div>
              {proposal.status === 'pending' && <div className="prose-proposal-actions">
                <button
                  className="chip"
                  disabled={busy}
                  onClick={() => setProposalDrafts((current) => {
                    const next = { ...current }
                    if (next[proposal.id]) delete next[proposal.id]
                    else next[proposal.id] = Object.fromEntries(proposal.changes.map((change) => [change.block_id, change.after]))
                    return next
                  })}
                >
                  {proposalDrafts[proposal.id] ? 'reset proposal' : 'edit proposal'}
                </button>
                <button
                  className="chip ok"
                  disabled={busy || drafts !== null}
                  onClick={() => void decideProseProposal(proposal, 'commit')}
                >
                  {proposalDrafts[proposal.id] ? 'commit edited proposal' : 'commit'}
                </button>
                <button
                  className="chip no"
                  disabled={busy}
                  onClick={() => void decideProseProposal(proposal, 'reject')}
                >
                  reject
                </button>
              </div>}
            </article>
            )
          })}
        </section>

      <div className="novel-actions">
        {drafts === null ? (
          <>
            <button
              className="chip ok"
              disabled={busy || derivedParts.every((part) => part.status === 'approved')}
              onClick={() => void verdict('approved')}
            >
              approve
            </button>
            <button
              className="chip no"
              disabled={busy || derivedParts.every((part) => part.status === 'rejected')}
              onClick={() => void verdict('rejected')}
            >
              reject
            </button>
            <button className="chip ghost" disabled={busy} onClick={startEditing}>
              edit by hand
            </button>
            {scene.kind !== 'frontmatter' && derivedParts.length === 1 && (
              <button
                className="chip ghost"
                disabled={busy}
                onClick={() => setShowDirection((s) => !s)}
              >
                re-roll…
              </button>
            )}
          </>
        ) : (
          <>
            <button className="chip ok" disabled={busy} onClick={() => void save()}>
              save
            </button>
            <button className="chip ghost" disabled={busy} onClick={discardDrafts}>
              discard
            </button>
            <span className="muted small">
              Hand edits are yours; they are marked and never overwritten by a
              re-roll of a different scene. Save also refreshes the complete
              private_manuscript_autosave.txt backup.
            </span>
          </>
        )}
      </div>

      {showDirection && plan && (
        <div className="reroll">
          <VoicePicker
            voice={{ ...(derived.voice ?? plan.voice), ...voice }}
            persons={plan.persons}
            tenses={plan.tenses}
            cast={plan.focal_candidates}
            onChange={(patch) => setVoice((v) => ({ ...v, ...patch }))}
          />
          <textarea
            className="direction"
            placeholder="What was wrong with this take? (optional — e.g. 'too much interiority; let the dialogue carry it')"
            value={direction}
            onChange={(e) => setDirection(e.target.value)}
          />
          <button className="stamp-button" onClick={() => void run(true)}>
            write it again
          </button>
          <p className="muted small">
            A re-roll replaces this scene’s prose in place, keeping its id, its
            verdict history and anything hanging off it.
          </p>
        </div>
      )}
    </div>
  )
}

function formatVoice(voice: Voice, plan: NovelizePlan): string {
  const person = plan.persons[voice.person] ?? voice.person
  const tense = plan.tenses[voice.tense] ?? voice.tense
  return voice.focal ? `${person}, ${tense}, following ${voice.focal}` : `${person}, ${tense}`
}

function sourceHint(src?: string[]): string {
  if (!src?.length) return ''
  return `from ${src.length} source turn${src.length > 1 ? 's' : ''}`
}
