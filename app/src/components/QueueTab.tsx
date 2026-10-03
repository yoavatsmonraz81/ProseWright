/** The review queue: manuscript TOC + PR cards + batch novelize.
 *
 * Lists what has been novelized. Expanding a row opens a side-by-side of the
 * log source and the novel prose (Phase 3.3 / 7.3). Batch runs pending scenes
 * in order and skips anything already filed (Phase 3.2).
 */

import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type {
  DerivedScene,
  LogSceneView,
  ManuscriptView,
  NovelizeBatchResult,
  ProseReviewSummary,
  SceneRow,
  SceneStatus,
} from '../types'

interface Props {
  novel: ManuscriptView | null
  scenes: SceneRow[]
  activeScene: number | null
  onPick: (msgId: number) => void
  onSetBookVoice: () => void
  onRefresh: () => void
  reviewRevision?: number
}

const ORDER = ['draft', 'approved', 'rejected'] as const

export function QueueTab({
  novel,
  scenes,
  activeScene,
  onPick,
  onSetBookVoice,
  onRefresh,
  reviewRevision = 0,
}: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchPhase, setBatchPhase] = useState('')
  const [batchResult, setBatchResult] = useState<NovelizeBatchResult | null>(null)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [maxScenes, setMaxScenes] = useState('3')
  const [maxRetries, setMaxRetries] = useState('2')
  const [reviewSummary, setReviewSummary] = useState<ProseReviewSummary | null>(null)
  const [reviewOnly, setReviewOnly] = useState(false)
  const [verifyOnly, setVerifyOnly] = useState(false)
  const [reviewError, setReviewError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api.proseReviewSummary().then((summary) => {
      if (active) { setReviewSummary(summary); setReviewError(null) }
    }).catch((error) => {
      if (active) setReviewError(error instanceof Error ? error.message : String(error))
    })
    return () => { active = false }
  }, [reviewRevision])

  if (!novel) {
    return (
      <div className="rail-body">
        <p className="muted pad">reading the manuscript…</p>
      </div>
    )
  }

  const total = novel.orchestration_total ?? scenes.length
  const pending = novel.orchestration_pending ?? (total - scenes.filter((s) => s.manuscript).length)
  const done = total - pending

  const runBatch = async (all = false) => {
    setBatchBusy(true)
    setBatchError(null)
    setBatchResult(null)
    setBatchPhase('starting…')
    try {
      const n = Number(maxScenes)
      const retries = Number(maxRetries)
      const result = await api.streamNovelizeBatch(
        {
          max_scenes: all ? undefined : (Number.isFinite(n) && n > 0 ? n : 3),
          max_retries: Number.isFinite(retries) && retries >= 0 ? retries : 2,
          continuity_chars: 3000,
        },
        (ev) => {
          if (ev.kind === 'phase' && ev.text) setBatchPhase(ev.text)
        },
      )
      setBatchResult(result)
      onRefresh()
    } catch (err) {
      setBatchError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBatchBusy(false)
      setBatchPhase('')
    }
  }

  if (!novel.scenes.length) {
    return (
      <div className="rail-body">
        <p className="muted pad">
          Nothing novelized yet. Open a scene on the <b>novel</b> layer, or run a
          short batch below.
        </p>
        <BatchControls
          pending={pending}
          busy={batchBusy}
          phase={batchPhase}
          maxScenes={maxScenes}
          onMaxScenes={setMaxScenes}
          maxRetries={maxRetries}
          onMaxRetries={setMaxRetries}
          onRun={(all) => void runBatch(all)}
          error={batchError}
          result={batchResult}
        />
        <p className="muted pad small">
          The book’s voice is {novel.voice_label}.{' '}
          <button className="link" onClick={onSetBookVoice}>
            change it
          </button>
        </p>
      </div>
    )
  }

  const rows = [...novel.scenes].sort((a, b) => a.start - b.start)
  const visibleRows = rows.filter((row) => {
    const counts = reviewSummary?.scenes[row.id]
    return (!reviewOnly || !!counts?.pending) && (!verifyOnly || !!counts?.verify)
  })
  const counts = ORDER.map((status) => ({
    status,
    n: rows.filter((r) => r.status === status).length,
  })).filter((c) => c.n)

  return (
    <div className="rail-body">
      <div className="queue-head">
        <p className="muted small">
          {done} of {total} scenes · {novel.stats.words?.toLocaleString() ?? 0} words
          {pending > 0 ? ` · ${pending} pending` : ''}
        </p>
        <p className="muted small">
          {counts.map((c) => `${c.n} ${c.status}`).join(' · ')}
          {novel.drifted ? ` · ${novel.drifted} drifted` : ''}
        </p>
        <p className="muted small">
          {novel.voice_label}{' '}
          <button className="link" onClick={onSetBookVoice}>
            change
          </button>
        </p>
        <div className="queue-review-summary">
          <strong>Prose review</strong>
          {reviewSummary && (
            <p className="muted small">
              {reviewSummary.total.toLocaleString()} proposals · {reviewSummary.totals.pending.toLocaleString()} pending · {reviewSummary.totals.verify} VERIFY · {reviewSummary.totals.committed} committed · {reviewSummary.totals.stale} stale · {reviewSummary.totals.rejected} rejected
            </p>
          )}
          {reviewError && <p className="error small">{reviewError}</p>}
          <label><input type="checkbox" checked={reviewOnly} onChange={(e) => setReviewOnly(e.target.checked)} /> scenes with pending proposals</label>
          <label><input type="checkbox" checked={verifyOnly} onChange={(e) => setVerifyOnly(e.target.checked)} /> VERIFY scenes</label>
        </div>
        <BatchControls
          pending={pending}
          busy={batchBusy}
          phase={batchPhase}
          maxScenes={maxScenes}
          onMaxScenes={setMaxScenes}
          maxRetries={maxRetries}
          onMaxRetries={setMaxRetries}
          onRun={(all) => void runBatch(all)}
          error={batchError}
          result={batchResult}
        />
      </div>

      <ul className="queue">
        {visibleRows.map((row) => {
          const scene = scenes.find((s) => s.start <= row.start && row.start <= s.end)
          const on = scene?.scene_id === activeScene
          const open = expanded === row.id
          return (
            <li key={row.id} className={on ? 'is-on' : ''}>
              <button
                onClick={() => {
                  setExpanded(open ? null : row.id)
                  onPick(row.start)
                }}
              >
                <span className={`dot status-${row.status}`} aria-hidden />
                <span className="queue-title">
                  {row.title || `msgs ${row.start}–${row.end}`}
                </span>
                <span className="queue-meta">
                  {row.words.toLocaleString()}w
                  {reviewSummary?.scenes[row.id]?.pending ? ` · ${reviewSummary.scenes[row.id].pending} review` : ''}
                  {reviewSummary?.scenes[row.id]?.verify ? ` · ${reviewSummary.scenes[row.id].verify} VERIFY` : ''}
                  {row.drift?.kind ? ` · ${row.drift.kind}` : ''}
                  {row.pinned ? ' · pinned' : ''}
                </span>
              </button>
              {open && (
                <QueuePrCard
                  sceneId={row.id}
                  status={row.status}
                  onChanged={() => {
                    onRefresh()
                  }}
                  onOpenNovel={() => onPick(row.start)}
                />
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function BatchControls({
  pending,
  busy,
  phase,
  maxScenes,
  onMaxScenes,
  maxRetries,
  onMaxRetries,
  onRun,
  error,
  result,
}: {
  pending: number
  busy: boolean
  phase: string
  maxScenes: string
  onMaxScenes: (v: string) => void
  maxRetries: string
  onMaxRetries: (v: string) => void
  onRun: (all: boolean) => void
  error: string | null
  result: NovelizeBatchResult | null
}) {
  return (
    <div className="queue-batch">
      <div className="queue-batch-row">
        <label>
          <span>batch next</span>
          <input
            value={maxScenes}
            disabled={busy}
            onChange={(e) => onMaxScenes(e.target.value)}
            title="Max scenes this run (resume skips done)"
          />
        </label>
        <label>
          <span>retries</span>
          <input
            value={maxRetries}
            disabled={busy}
            onChange={(e) => onMaxRetries(e.target.value)}
            title="Retries per failed scene"
          />
        </label>
        <button
          type="button"
          className="btn"
          disabled={busy || pending <= 0}
          onClick={() => onRun(false)}
        >
          {busy ? 'novelizing…' : 'run next'}
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy || pending <= 0}
          onClick={() => onRun(true)}
          title="Run every pending scene; progress is checkpointed after each scene"
        >
          run all ({pending})
        </button>
      </div>
      {busy && phase && <p className="muted small">{phase}</p>}
      {error && <p className="error">{error}</p>}
      {result && (
        <p className="muted small">
          last run: {result.counts.novelized} novelized · {result.counts.failed}{' '}
          failed · {result.counts.skipped} skipped
          {result.campaign?.status ? ` · ${result.campaign.status}` : ''}
        </p>
      )}
      {result?.campaign?.assembled && (
        <p className="muted small">assembled: {result.campaign.assembled}</p>
      )}
    </div>
  )
}

function QueuePrCard({
  sceneId,
  status,
  onChanged,
  onOpenNovel,
}: {
  sceneId: string
  status: SceneStatus
  onChanged: () => void
  onOpenNovel: () => void
}) {
  const [logSide, setLogSide] = useState<LogSceneView | null>(null)
  const [novelSide, setNovelSide] = useState<DerivedScene | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const [log, novel] = await Promise.all([
          api.sceneLog(sceneId),
          api.scene(sceneId, 'manuscript'),
        ])
        if (!live) return
        setLogSide(log)
        setNovelSide(novel)
      } catch (err) {
        if (!live) return
        setError(err instanceof ApiError ? err.message : String(err))
      } finally {
        if (live) setLoading(false)
      }
    })()
    return () => {
      live = false
    }
  }, [sceneId])

  const setStatus = async (next: SceneStatus) => {
    setBusy(true)
    setError(null)
    try {
      setNovelSide(await api.setSceneStatus(sceneId, next))
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="queue-pr">
      {error && <p className="error">{error}</p>}
      {loading || !logSide || !novelSide ? (
        <p className="muted small pad">loading PR…</p>
      ) : (
        <>
          <div className="queue-pr-cols">
            <section>
              <h5>log</h5>
              <pre className="queue-pr-body">
                {logSide.text || 'Not in the log — front matter.'}
              </pre>
            </section>
            <section>
              <h5>novel</h5>
              <pre className="queue-pr-body">{novelSide.text}</pre>
            </section>
          </div>
          <div className="queue-pr-actions">
            <button
              type="button"
              className="btn approve"
              disabled={busy || status === 'approved'}
              onClick={() => void setStatus('approved')}
            >
              approve
            </button>
            <button
              type="button"
              className="btn reject"
              disabled={busy || status === 'rejected'}
              onClick={() => void setStatus('rejected')}
            >
              reject
            </button>
            <button type="button" className="link" onClick={onOpenNovel}>
              open on novel page
            </button>
          </div>
        </>
      )}
    </div>
  )
}
