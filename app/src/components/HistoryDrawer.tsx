import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import type { HistoryEntry, SceneRow } from '../types'

interface Props {
  scenes: SceneRow[]
  activeScene: number | null
  onClose: () => void
  onJump: (msgId: number) => void
  onUndone: (focus: number | null) => Promise<void>
}

function overlaps(entry: HistoryEntry, scene: SceneRow): boolean {
  if (entry.changed_from == null || entry.changed_to == null) return false
  return entry.changed_from <= scene.end && entry.changed_to >= scene.start
}

const isLog = (entry: HistoryEntry) => (entry.layer ?? 'log') === 'log'

// Manuscript categories sit apart from the log operators in the filter.
const MANUSCRIPT_CATEGORIES = ['committed', 'manually edited']

function sceneLabel(scene: SceneRow): string {
  const title = scene.episode_title || scene.location || scene.beat_title || `Scene ${scene.scene_id}`
  return `S${String(scene.scene_id).padStart(2, '0')} · ${title}`
}

export function HistoryDrawer({ scenes, activeScene, onClose, onJump, onUndone }: Props) {
  const [entries, setEntries] = useState<HistoryEntry[]>([])
  const [sceneFilter, setSceneFilter] = useState(
    activeScene == null ? 'all' : String(activeScene),
  )
  const [operator, setOperator] = useState('all')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [undoing, setUndoing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.history(500)
      setEntries(result.entries)
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const operators = useMemo(
    () => [...new Set(entries.filter(isLog).map((entry) => entry.operator))].sort(),
    [entries],
  )
  const chosenScene =
    sceneFilter === 'all'
      ? null
      : scenes.find((scene) => scene.scene_id === Number(sceneFilter)) ?? null
  const filtered = entries.filter((entry) => {
    if (chosenScene && !overlaps(entry, chosenScene)) return false
    if (operator === 'log' && !isLog(entry)) return false
    if (operator === 'manuscript' && isLog(entry)) return false
    if (!['all', 'log', 'manuscript'].includes(operator) && entry.operator !== operator) return false
    return true
  })
  // Undo restores the working log, so only log entries are candidates.
  const latest = entries.find(isLog) ?? null
  const canUndo = latest?.event === 'commit' && Boolean(latest.backup)

  const undoLatest = async () => {
    if (!latest || !canUndo || undoing) return
    const label = latest.note || latest.operator
    if (!window.confirm(
      `Restore the complete working log to the backup from before history #${latest.id} (${label})? This undoes the latest accepted log change.`,
    )) return
    setUndoing(true)
    setError(null)
    try {
      await api.undoEdits(latest.id)
      await onUndone(latest.changed_from)
      await load()
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : String(reason))
    } finally {
      setUndoing(false)
    }
  }

  return (
    <div className="sheet-scrim history-scrim" onClick={onClose}>
      <aside className="sheet history-drawer" onClick={(event) => event.stopPropagation()}>
        <header>
          <h3>Edit history</h3>
          <button className="chip ghost" disabled={undoing} onClick={onClose}>close</button>
        </header>
        <p className="muted small">
          Accepted log transformations and interludes, plus manuscript changes: prose-review
          proposals you committed and paragraphs you edited by hand.
        </p>

        <div className="history-filters">
          <label>
            <span>scene</span>
            <select value={sceneFilter} onChange={(event) => setSceneFilter(event.target.value)}>
              <option value="all">all scenes</option>
              {scenes.filter((scene) => scene.start >= 0).map((scene) => (
                <option key={scene.scene_id} value={scene.scene_id}>
                  {sceneLabel(scene)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>category</span>
            <select value={operator} onChange={(event) => setOperator(event.target.value)}>
              <option value="all">everything</option>
              <optgroup label="manuscript">
                <option value="manuscript">all manuscript changes</option>
                {MANUSCRIPT_CATEGORIES.map((name) => <option key={name} value={name}>{name}</option>)}
              </optgroup>
              <optgroup label="log">
                <option value="log">all log changes</option>
                {operators.map((name) => <option key={name} value={name}>{name}</option>)}
              </optgroup>
            </select>
          </label>
        </div>

        <div className="history-summary">
          <span className="muted small">
            {loading ? 'reading history…' : `${filtered.length} of ${entries.length} entries`}
          </span>
          <button
            type="button"
            className="chip no"
            disabled={!canUndo || undoing}
            onClick={() => void undoLatest()}
            title={canUndo ? 'Restore the backup taken before the latest accepted log edit' : 'The latest history event is not an undoable commit'}
          >
            {undoing ? 'restoring…' : 'undo latest log change'}
          </button>
        </div>
        {error && <p className="error">{error}</p>}

        <ol className="history-list">
          {filtered.map((entry) => {
            const open = expanded === entry.id
            return (
              <li key={entry.id} className={`history-entry event-${entry.event}`}>
                <button
                  type="button"
                  className="history-entry-head"
                  onClick={() => setExpanded(open ? null : entry.id)}
                >
                  <span className={`dot ${entry.event === 'undo' ? 'status-rejected' : 'status-approved'}`} />
                  <span className="history-entry-title">
                    #{entry.id} · {entry.event === 'undo' ? 'undo' : entry.operator}
                    {!isLog(entry) && ` · ${entry.scene_title || entry.scene_id}`}
                  </span>
                  <time>{entry.created ? new Date(entry.created).toLocaleString() : ''}</time>
                </button>
                <p className="history-entry-note">
                  {entry.note || entry.locator || 'No note'}
                  {isLog(entry) && entry.changed_from != null && (
                    <button className="link" onClick={() => onJump(entry.changed_from!)}>
                      {entry.changed_from === entry.changed_to
                        ? `msg ${entry.changed_from}`
                        : `msgs ${entry.changed_from}–${entry.changed_to}`}
                    </button>
                  )}
                </p>
                {open && (
                  <div className="history-detail">
                    {entry.locator && <p className="provenance">{entry.locator}</p>}
                    {!isLog(entry) && entry.backup && (
                      <p className="provenance">backup before this change: {entry.backup}</p>
                    )}
                    {entry.edits.length === 0 ? (
                      <p className="muted small">No per-message diff was recorded for this event.</p>
                    ) : entry.edits.map((edit, index) => (
                      <section key={`${entry.id}-${index}`} className="history-edit">
                        <h4>
                          {isLog(entry)
                            ? `msg ${edit.msg_id} · ${edit.speaker} · ${edit.kind}`
                            : `paragraph ${edit.block_id ?? ''} · ${edit.kind}`}
                        </h4>
                        <div className="history-diff">
                          <div><span>before</span><pre>{edit.before || '—'}</pre></div>
                          <div><span>after</span><pre>{edit.after || '—'}</pre></div>
                        </div>
                      </section>
                    ))}
                  </div>
                )}
              </li>
            )
          })}
          {!loading && filtered.length === 0 && (
            <li className="muted small pad">No history entries match these filters.</li>
          )}
        </ol>
      </aside>
    </div>
  )
}
