/** The fork pane — where a proposed change is read and answered.
 *
 * A restyled span, an injected turn, a chorus interlude and a drifted manuscript
 * scene are one situation seen several times: a text, a version of that text,
 * and a decision. Log proposals use the diff below; manuscript drift uses
 * rebase / pin / fork.
 *
 * With nothing pending, the pane is the verb list for the layer on screen —
 * which is also how the layer rule becomes visible: switch to novel and the
 * log's operators are simply not there.
 */

import { useEffect, useState } from 'react'
import type { EditDiff, Layer, ManuscriptView, Proposal, SceneRow, StampReport, SweepReport, WeedReport } from '../types'
import type { Verb } from '../lib/verbs'

interface Props {
  layer: Layer
  verbs: Verb[]
  proposal: Proposal | null
  sweep: SweepReport | null
  weeds: WeedReport | null
  stamps: StampReport | null
  novel: ManuscriptView | null
  scene: SceneRow | null
  compressionStatus: string
  rhythmStatus: string
  busy: boolean
  error: string | null
  onVerb: (verb: string) => void
  onAccept: (allowFlagged: boolean) => void
  onAcceptOne: (index: number, allowFlagged: boolean) => void
  onReject: () => void
  onDrop: (msgId: number) => void
  onDropAt: (index: number) => void
  onJump: (msgId: number) => void
  onClearSweep: () => void
  onClearWeeds: () => void
  onProposeWeeds: () => void
  onClearStamps: () => void
  onProposeStamps: () => void
  onRebaseScene: (id: string) => void
  onPinScene: (id: string) => void
  onCompressScene: () => void
  onSmoothScene: () => void
}

const VERDICT_WORD: Record<string, string> = {
  CLEAN: 'nothing downstream contradicts the change',
  REVIEW: 'later turns worth reading again',
  BREAK: 'later turns that now contradict the change',
}

export function ForkPane({
  layer,
  verbs,
  proposal,
  sweep,
  weeds,
  stamps,
  novel,
  scene,
  compressionStatus,
  rhythmStatus,
  busy,
  error,
  onVerb,
  onAccept,
  onAcceptOne,
  onReject,
  onDrop,
  onDropAt,
  onJump,
  onClearSweep,
  onClearWeeds,
  onProposeWeeds,
  onClearStamps,
  onProposeStamps,
  onRebaseScene,
  onPinScene,
  onCompressScene,
  onSmoothScene,
}: Props) {
  const [allowFlagged, setAllowFlagged] = useState(false)
  useEffect(() => setAllowFlagged(false), [proposal?.edits?.length])
  const drifted =
    novel?.scenes.filter(
      (s) => s.drift?.kind && s.drift.kind !== '' && s.drift.kind !== 'pinned',
    ) ?? []
  const pinned = novel?.scenes.filter((s) => s.drift?.kind === 'pinned' || s.pinned) ?? []

  if (proposal?.kind) {
    return (
      <ProposalReview
        proposal={proposal}
        busy={busy}
        error={error}
        allowFlagged={allowFlagged}
        onAllowFlagged={setAllowFlagged}
        onAccept={onAccept}
        onAcceptOne={onAcceptOne}
        onReject={onReject}
        onDrop={onDrop}
        onDropAt={onDropAt}
        onJump={onJump}
      />
    )
  }

  if (sweep) {
    return <SweepView report={sweep} onJump={onJump} onClose={onClearSweep} />
  }

  if (weeds) {
    return (
      <WeedView
        report={weeds}
        busy={busy}
        error={error}
        onJump={onJump}
        onClose={onClearWeeds}
        onPropose={onProposeWeeds}
      />
    )
  }

  if (stamps) {
    return (
      <StampView
        report={stamps}
        busy={busy}
        error={error}
        onJump={onJump}
        onClose={onClearStamps}
        onPropose={onProposeStamps}
      />
    )
  }

  return (
    <div className="slot-body">
      {error && <p className="error">{error}</p>}

      {(drifted.length > 0 || pinned.length > 0) && (
        <>
          <h4 className="rail-sub">manuscript drift</h4>
          <ul className="drift-list">
            {drifted.map((s) => (
              <li key={s.id}>
                <button className="link" onClick={() => onJump(s.start)}>
                  {s.title || `msgs ${s.start}–${s.end}`}
                </button>
                <span className="muted small"> · {s.drift.kind}</span>
                <div className="drift-actions">
                  <button
                    type="button"
                    className="link"
                    disabled={busy}
                    onClick={() => onRebaseScene(s.id)}
                  >
                    rebase
                  </button>
                  <button
                    type="button"
                    className="link"
                    disabled={busy}
                    onClick={() => onPinScene(s.id)}
                  >
                    pin
                  </button>
                </div>
              </li>
            ))}
            {pinned.map((s) => (
              <li key={`pin-${s.id}`} className="muted small">
                pinned: {s.title || s.id}{' '}
                <button
                  type="button"
                  className="link"
                  disabled={busy}
                  onClick={() => onRebaseScene(s.id)}
                >
                  rebase
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      <p className="muted">
        {drifted.length === 0
          ? `Nothing is waiting for a verdict. These are the operators the engine will run against the ${layer === 'manuscript' ? 'novel' : layer} layer.`
          : 'Resolve drift above, or run a log-layer verb below.'}
      </p>
      {layer === 'manuscript' && (
        <section className="fork-manuscript-tools" aria-label="Manuscript scene tools">
          <h4 className="rail-sub">manuscript scene</h4>
          <button
            type="button"
            className="verb-line"
            disabled={busy || !scene || scene.kind === 'frontmatter' || (!scene.manuscript && !scene.manuscript_parts?.length)}
            onClick={onSmoothScene}
            title="Propose repairs for accidental fragments, staccato phrasing, and excessive em-dash chaining. Nothing is committed automatically."
          >
            <span className="verb-name">repair phrasing</span>
            <span className="verb-what">join accidental fragments and tame repeated em dashes</span>
          </button>
          <p className="hint">
            Manuscript only · keeps deliberate pauses, dialogue, emphasis, and paragraph boundaries · review every proposal beneath the prose.
          </p>
          {rhythmStatus && <p className="provenance">{rhythmStatus}</p>}
          <button
            type="button"
            className="verb-line"
            disabled={busy || !scene || scene.kind === 'frontmatter' || (!scene.manuscript && !scene.manuscript_parts?.length)}
            onClick={onCompressScene}
            title="Propose removals of redundant narration in the current scene. Nothing is committed automatically."
          >
            <span className="verb-name">compress scene</span>
            <span className="verb-what">prune repeated narration and explanation</span>
          </button>
          <p className="hint">
            Manuscript only · preserves dialogue and emphasis · review every proposal beneath the prose.
          </p>
          {compressionStatus && <p className="provenance">{compressionStatus}</p>}
        </section>
      )}
      <h4 className="rail-sub">verbs</h4>
      <ul className="verb-list">
        {verbs.map((verb) => (
          <li key={verb.name} className={`verb ${verb.state}`}>
            <button
              className="verb-line"
              disabled={verb.state !== 'ready'}
              onClick={() => onVerb(verb.name)}
            >
              <span className="verb-name">{verb.name}</span>
              <span className="verb-what">{verb.what}</span>
            </button>
            {verb.reason && <p className="hint">{verb.reason}</p>}
          </li>
        ))}
        {verbs.length === 0 && (
          <li className="muted">The engine reports no operators for this layer.</li>
        )}
      </ul>
    </div>
  )
}

function ProposalReview({
  proposal,
  busy,
  error,
  allowFlagged,
  onAllowFlagged,
  onAccept,
  onAcceptOne,
  onReject,
  onDrop,
  onDropAt,
  onJump,
}: {
  proposal: Proposal
  busy: boolean
  error: string | null
  allowFlagged: boolean
  onAllowFlagged: (value: boolean) => void
  onAccept: (allowFlagged: boolean) => void
  onAcceptOne: (index: number, allowFlagged: boolean) => void
  onReject: () => void
  onDrop: (msgId: number) => void
  onDropAt: (index: number) => void
  onJump: (msgId: number) => void
}) {
  const [cursor, setCursor] = useState(0)
  const edits = proposal.edits ?? []
  const reviewTotal = proposal.meta?.review_total ?? edits.length
  const serial =
    (proposal.operator === 'weed' || proposal.operator === 'copyedit') && reviewTotal > 1
  useEffect(() => {
    setCursor((current) => Math.max(0, Math.min(current, edits.length - 1)))
  }, [edits.length])
  const current = serial ? edits[cursor] : undefined
  const shownEdits = current ? [current] : edits
  const flagged = serial
    ? (current?.hard_flagged ? 1 : 0)
    : proposal.hard_flagged ?? 0
  const single = edits.length < 2

  return (
    <div className="slot-body">
      <div className="contract-head">
        <span className="badge proposed">{proposal.operator ?? 'proposal'}</span>
        <span className="muted small">{proposal.locator}</span>
      </div>
      {proposal.note && <h3 className="contract-title">{proposal.note}</h3>}
      {proposal.operator === 'weed' && proposal.meta && (
        <p className="hint">
          {proposal.meta.phrases?.length
            ? proposal.meta.phrases.join(', ')
            : 'no phrases'}
          {proposal.meta.hit_count != null ? ` · ${proposal.meta.hit_count} sentences` : ''}
          {proposal.meta.mechanical != null
            ? ` · ${proposal.meta.mechanical} pulled without a rewrite`
            : ''}
          {proposal.meta.rewritten != null && proposal.meta.rewritten > 0
            ? ` · ${proposal.meta.rewritten} rewritten`
            : ''}
        </p>
      )}
      {serial && current && (
        <p className="provenance">
          reviewing {cursor + 1} of {edits.length} remaining ·{' '}
          {(proposal.meta?.review_accepted ?? 0) + (proposal.meta?.review_rejected ?? 0)} of{' '}
          {reviewTotal} decided
        </p>
      )}
      <p className="provenance">
        {proposal.operator === 'remove'
          ? `${edits.length} message${edits.length === 1 ? '' : 's'} will be taken out`
          : `${edits.length} message${edits.length === 1 ? '' : 's'} rewritten`}{' '}
        · nothing written yet
      </p>
      {proposal.operator === 'remove' && proposal.meta?.sweep && (
        <p className="hint">
          After accept, sweep will look downstream for turns that leaned on what was cut.
        </p>
      )}

      {proposal.advisory && (
        <p className="hint">
          The model read the direction and left the prose alone — it judged the turns
          already the way you asked for. A sharper note, or a different span, is the
          next move.
        </p>
      )}

      {flagged > 0 && (
        <p className="warn">
          {flagged} rewrite{flagged === 1 ? '' : 's'} disturbed another character’s
          quoted line. Accepting is refused until those are dropped, or you say plainly
          that you have read them.
        </p>
      )}

      {shownEdits.map((edit) => (
        <EditCard
          key={`${edit.msg_id}-${edit.kind}`}
          edit={edit}
          busy={busy}
          droppable={!serial && !single}
          onDrop={onDrop}
          onJump={onJump}
        />
      ))}

      {error && <p className="error">{error}</p>}

      {edits.length > 0 && (
        <>
          {flagged > 0 && (
            <label className="allow-flagged">
              <input
                type="checkbox"
                checked={allowFlagged}
                onChange={(e) => onAllowFlagged(e.target.checked)}
              />
              <span>I have read the flagged rewrites and they are right</span>
            </label>
          )}
          <div className="verdict">
            {serial && current ? (
              <>
                <button
                  className="btn approve"
                  disabled={busy || (flagged > 0 && !allowFlagged)}
                  onClick={() => onAcceptOne(current.index ?? cursor, allowFlagged)}
                >
                  accept this
                </button>
                <button
                  className="btn reject"
                  disabled={busy}
                  onClick={() => onDropAt(current.index ?? cursor)}
                >
                  reject this
                </button>
                <button
                  className="btn ghost"
                  disabled={busy || edits.length < 2}
                  onClick={() => setCursor((cursor + 1) % edits.length)}
                >
                  next
                </button>
              </>
            ) : (
              <>
                <button
                  className="btn approve"
                  disabled={busy || (flagged > 0 && !allowFlagged)}
                  onClick={() => onAccept(allowFlagged)}
                >
                  accept
                </button>
                <button className="btn reject" disabled={busy} onClick={onReject}>
                  reject
                </button>
              </>
            )}
          </div>
          <p className="muted small">
            {proposal.operator === 'remove'
              ? 'Accepting backs the log up first and deletes these turns. Later ids shift down; gold and the voice bank move with them. Rejecting writes nothing.'
              : serial
                ? 'Each acceptance is its own backed-up commit. Reject removes only the current item; next leaves it undecided.'
                : 'Accepting backs the log up first and rewrites only the message bodies it shows. Rejecting writes nothing and throws the proposal away.'}
          </p>
        </>
      )}

      {edits.length === 0 && (
        <div className="verdict">
          <button className="btn reject" disabled={busy} onClick={onReject}>
            clear it
          </button>
        </div>
      )}
    </div>
  )
}

function EditCard({
  edit,
  busy,
  droppable,
  onDrop,
  onJump,
}: {
  edit: EditDiff
  busy: boolean
  droppable: boolean
  onDrop: (msgId: number) => void
  onJump: (msgId: number) => void
}) {
  const [whole, setWhole] = useState(false)
  const inject = edit.kind === 'inject'
  const removing = edit.kind === 'remove'

  return (
    <section className="edit-card">
      <div className="edit-head">
        <button className="link" onClick={() => onJump(edit.msg_id)}>
          {inject ? `after msg ${edit.msg_id}` : `msg ${edit.msg_id}`}
        </button>
        <span className="muted small">{edit.speaker}</span>
        <span className="edit-tools">
          <button className="chip ghost tiny" onClick={() => setWhole((w) => !w)}>
            {whole ? 'diff' : 'whole'}
          </button>
          {droppable && (
            <button
              className="chip ghost tiny"
              disabled={busy}
              title="leave this message out of the change"
              onClick={() => onDrop(edit.msg_id)}
            >
              ✕
            </button>
          )}
        </span>
      </div>

      {edit.header && (
        <p className={`edit-header ${edit.header_preserved ? '' : 'changed'}`}>
          {edit.header.trim()}
          <span className="muted small">
            {edit.header_preserved
              ? ' — kept exactly as it was'
              : ' — the header itself changed, which the operators never do'}
          </span>
        </p>
      )}

      {edit.flags.map((flag) => (
        <p className={flag.startsWith('ALTERED') ? 'error' : 'warn'} key={flag}>
          {flag}
        </p>
      ))}

      {whole ? (
        <>
          {!inject && (
            <p className="diff-side">
              <span className="diff-label">before</span>
              {edit.before}
            </p>
          )}
          {!removing && (
            <p className="diff-side">
              <span className="diff-label">{inject ? 'the new passage' : 'after'}</span>
              {edit.after}
            </p>
          )}
          {removing && (
            <p className="diff-side">
              <span className="diff-label">this turn will be taken out</span>
            </p>
          )}
        </>
      ) : (
        <p className="diff">
          {edit.hunks.map((hunk, i) => (
            <span
              key={i}
              className={hunk.kind === 'same' ? undefined : `diff-${hunk.kind}`}
            >
              {hunk.text}
            </span>
          ))}
        </p>
      )}
    </section>
  )
}

function SweepView({
  report,
  onJump,
  onClose,
}: {
  report: SweepReport
  onJump: (msgId: number) => void
  onClose: () => void
}) {
  return (
    <div className="slot-body">
      <div className="contract-head">
        <span className="badge proposed">sweep</span>
        <button className="chip ghost tiny" onClick={onClose}>
          ↩
        </button>
      </div>
      <h3 className="contract-title">{report.note || report.operator}</h3>
      <p className="provenance">
        msgs {report.changed_from}–{report.changed_to} changed · read forward from msg{' '}
        {report.sweep_from}
      </p>
      <p className="hint">
        {VERDICT_WORD[report.verdict] ?? report.verdict} · {report.clean_count} turns
        read and cleared.
      </p>
      <ul className="verb-list">
        {report.hits.map((hit) => (
          <li key={hit.msg_id} className="verb">
            <button className="verb-line" onClick={() => onJump(hit.msg_id)}>
              <span className="verb-name">{hit.verdict.toLowerCase()}</span>
              <span className="verb-what">
                msg {hit.msg_id} · {hit.speaker}
              </span>
            </button>
            <p className="hint">{hit.reason}</p>
          </li>
        ))}
        {report.hits.length === 0 && (
          <li className="muted">Nothing later in the log leans on what changed.</li>
        )}
      </ul>
      <p className="muted small">
        A sweep is a reading, not a rewrite. Fixing one of these is a restyle of its
        own, against its own note.
      </p>
    </div>
  )
}

function WeedView({
  report,
  busy,
  error,
  onJump,
  onClose,
  onPropose,
}: {
  report: WeedReport
  busy: boolean
  error: string | null
  onJump: (msgId: number) => void
  onClose: () => void
  onPropose: () => void
}) {
  return (
    <div className="slot-body">
      <div className="contract-head">
        <span className="badge proposed">weed</span>
        <button className="chip ghost tiny" onClick={onClose}>
          ↩
        </button>
      </div>
      <h3 className="contract-title">
        {report.hit_count} sentence{report.hit_count === 1 ? '' : 's'} with a watchlist phrase
      </h3>
      <p className="provenance">
        {report.locator}
        {report.phrases.length ? ` · ${report.phrases.join(', ')}` : ''}
      </p>
      <p className="hint">
        A scan, not a rewrite. Propose pulls and each infected turn lands in this
        pane as a diff — accept, drop, or reject.
      </p>
      <ul className="verb-list">
        {report.hits.map((hit, i) => (
          <li key={`${hit.msg_id}-${hit.phrase_id}-${i}`} className="verb">
            <button className="verb-line" onClick={() => onJump(hit.msg_id)}>
              <span className="verb-name">{hit.phrase_id}</span>
              <span className="verb-what">
                msg {hit.msg_id} · {hit.speaker}
                {hit.source_id ? ` · ${hit.source_id}` : ''}
              </span>
            </button>
            <p className="hint">{hit.sentence}</p>
          </li>
        ))}
        {report.hits.length === 0 && (
          <li className="muted">Nothing in this stretch matches the weed list.</li>
        )}
      </ul>
      {error && <p className="error">{error}</p>}
      {report.hits.length > 0 && (
        <div className="verdict">
          <button className="btn approve" disabled={busy} onClick={onPropose}>
            propose pulls
          </button>
        </div>
      )}
    </div>
  )
}

function StampView({
  report,
  busy,
  error,
  onJump,
  onClose,
  onPropose,
}: {
  report: StampReport
  busy: boolean
  error: string | null
  onJump: (msgId: number) => void
  onClose: () => void
  onPropose: () => void
}) {
  const rows = report.propose.slice(0, 80)
  return (
    <div className="slot-body">
      <div className="contract-head">
        <span className="badge proposed">stamps</span>
        <button className="chip ghost tiny" onClick={onClose}>
          ↩
        </button>
      </div>
      <h3 className="contract-title">
        {report.propose_count} turn{report.propose_count === 1 ? '' : 's'} missing a header
      </h3>
      <p className="provenance">
        {report.locator} · {report.complete}/{report.count} complete · {report.inherited}{' '}
        inherited
      </p>
      <p className="hint">
        A walk, not a rewrite. Metadata is already written to the sidecar.
        Propose headers and each CHAR turn lands here as a diff.
      </p>
      <ul className="verb-list">
        {rows.map((row) => (
          <li key={`${row.uid}-${row.msg_id}`} className="verb">
            <button className="verb-line" onClick={() => onJump(row.msg_id)}>
              <span className="verb-name">{row.speaker}</span>
              <span className="verb-what">msg {row.msg_id}</span>
            </button>
            <p className="hint">
              {[row.time, row.date, row.location].filter(Boolean).join(' · ') || 'incomplete'}
            </p>
          </li>
        ))}
        {report.propose_count === 0 && (
          <li className="muted">Every CHAR turn in this stretch already has a full header.</li>
        )}
        {report.propose_count > rows.length && (
          <li className="muted">…and {report.propose_count - rows.length} more.</li>
        )}
      </ul>
      {error && <p className="error">{error}</p>}
      {report.propose_count > 0 && (
        <div className="verdict">
          <button className="btn approve" disabled={busy} onClick={onPropose}>
            propose headers
          </button>
        </div>
      )}
    </div>
  )
}
