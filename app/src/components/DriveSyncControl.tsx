/** Google Drive sync — the workspace travels between laptop and workstation.
 *
 * Lives in the status bar next to the model switch. The engine does the
 * refusing: push is blocked while Drive holds the other machine's unpulled
 * work, pull is blocked while this machine holds unpushed work. Forcing either
 * direction is offered only after naming the files it would overwrite.
 */

import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { DriveJob, DriveProgress, DriveStatus } from '../types'

const LABEL: Record<DriveStatus['kind'], string> = {
  in_sync: 'in sync',
  local_ahead: 'unpushed changes',
  drive_ahead: 'Drive is newer',
  diverged: 'both changed',
  drive_empty: 'not pushed yet',
  unlinked: 'not linked yet',
  no_rclone: 'rclone missing',
  no_remote: 'not set up',
  drive_error: 'unreachable',
}

function when(iso: string | undefined): string {
  if (!iso) return ''
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} h ago`
  return new Date(iso).toLocaleString()
}

function mb(bytes: number | null | undefined): string {
  return `${((bytes ?? 0) / 1048576).toFixed(1)} MB`
}

function eta(seconds: number | null | undefined): string {
  if (seconds == null) return ''
  if (seconds < 60) return `${Math.round(seconds)} s left`
  return `${Math.round(seconds / 60)} min left`
}

function Progress({ job }: { job: DriveJob }) {
  const p: DriveProgress | null = job.progress
  const total = p?.totalBytes ?? 0
  // Before rclone knows the totals (listing, hashing) show an indeterminate bar.
  const pct = total > 0 ? Math.min(100, Math.round(((p?.bytes ?? 0) / total) * 100)) : null
  return (
    <div className="drive-progress">
      <div className="drive-progress-head">
        <span>{job.direction === 'pull' ? 'pulling' : 'pushing'} · {job.phase}</span>
        <span>{pct != null ? `${pct}%` : ''}</span>
      </div>
      <div className={`drive-bar${pct == null ? ' indeterminate' : ''}`}>
        <div style={{ width: pct == null ? undefined : `${pct}%` }} />
      </div>
      {p && (
        <p className="muted small">
          {mb(p.bytes)} of {mb(p.totalBytes)} · {p.transfers ?? 0} of {p.totalTransfers ?? 0} files
          {p.speed ? ` · ${mb(p.speed)}/s` : ''}
          {p.eta != null ? ` · ${eta(p.eta)}` : ''}
        </p>
      )}
      {!!p?.current.length && <p className="muted small drive-current">{p.current.join(', ')}</p>}
    </div>
  )
}

function Files({ title, files }: { title: string; files: string[] }) {
  if (!files.length) return null
  return (
    <div className="drive-files">
      <span>{title} ({files.length})</span>
      <ul>
        {files.slice(0, 12).map((f) => <li key={f}>{f}</li>)}
        {files.length > 12 && <li className="muted">…and {files.length - 12} more</li>}
      </ul>
    </div>
  )
}

export function DriveSyncControl() {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<DriveStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [job, setJob] = useState<DriveJob | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const refresh = async () => {
    try {
      setStatus(await api.driveStatus())
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  useEffect(() => {
    void refresh()
    // Pick up a push/pull already running (e.g. after a page reload).
    void api.driveJob().then((j) => j.running && setJob(j)).catch(() => undefined)
  }, [])

  // Poll the running job once a second; settle when it finishes.
  useEffect(() => {
    if (!job?.running) return
    const timer = window.setInterval(async () => {
      let next: DriveJob
      try {
        next = await api.driveJob()
      } catch {
        return
      }
      setJob(next)
      if (next.running) return
      window.clearInterval(timer)
      finish(next)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [job?.running])

  function finish(done: DriveJob) {
    if (done.error) {
      setError(done.error)
      void refresh()
      return
    }
    if (done.result?.status) setStatus(done.result.status)
    if (done.direction === 'push') {
      setNote('Pushed to Drive.')
      return
    }
    const changed = (done.result?.replaced?.length ?? 0) + (done.result?.removed?.length ?? 0)
    if (changed) {
      setNote(`Pulled ${changed} file(s). Reloading\u2026`)
      // Every pane caches what it read; start clean on the pulled data.
      window.setTimeout(() => window.location.reload(), 900)
    } else {
      setNote('Already up to date.')
    }
  }

  useEffect(() => {
    if (!open) return
    void refresh()
    const onDoc = (e: MouseEvent) => {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  async function run(direction: 'push' | 'pull', force = false) {
    if (force) {
      const files = direction === 'push' ? status?.drive_changes : status?.local_changes
      const what = direction === 'push'
        ? 'Overwrite the Drive copy of these files with this machine’s version'
        : 'Replace this machine’s copy of these files with Drive’s version (local copies are backed up first)'
      if (!window.confirm(`${what}?\n\n${(files ?? []).slice(0, 20).join('\n')}`)) return
    }
    setError(null)
    setNote(null)
    try {
      const started = await api.driveStart(direction, force)
      setJob(started.job)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      await refresh()
    }
  }

  const busy = Boolean(job?.running)
  const kind = status?.kind
  const tone = kind === 'in_sync' ? 'ok' : kind === 'diverged' || kind === 'drive_error' ? 'no' : 'warn'
  const conflicted = kind === 'diverged' || kind === 'unlinked'

  return (
    <div className="model-control drive-control" ref={panelRef}>
      <button
        type="button"
        className={`model-chip ${tone}`}
        onClick={() => setOpen((v) => !v)}
        title={status?.remote}
      >
        drive · {busy
          ? `${job!.direction}ing${job!.progress?.totalBytes ? ` ${Math.round(((job!.progress.bytes ?? 0) / job!.progress.totalBytes) * 100)}%` : '…'}`
          : kind ? LABEL[kind] : '…'}
      </button>

      {open && (
        <div className="model-panel drive-panel">
          <header>
            <h4>Google Drive sync</h4>
            <span className="muted">{status?.machine}</span>
          </header>

          {status?.drive && (
            <p className="small">
              Drive: pushed from <b>{status.drive.machine}</b> {when(status.drive.pushed_at)}
            </p>
          )}
          {status?.detail && <p className="muted small">{status.detail}</p>}
          {kind === 'no_rclone' && (
            <p className="small">Install rclone: <code>sudo apt install rclone</code></p>
          )}
          {kind === 'no_remote' && (
            <p className="small">
              Link your Google account once: <code>rclone config create gdrive drive</code>
            </p>
          )}
          {kind === 'diverged' && (
            <p className="warn small">
              Both machines changed files since the last sync. Pick which side wins
              for everything below — or push/pull the other way first if one list is empty of
              anything you care about.
            </p>
          )}
          {kind === 'unlinked' && (
            <p className="warn small">
              This machine has never synced and its workspace differs from Drive. Pull to take
              Drive&apos;s version (yours is backed up), or push to replace Drive&apos;s.
            </p>
          )}

          {status && kind !== 'unlinked' && (
            <>
              <Files title="changed here, not on Drive" files={status.local_changes} />
              <Files title="changed on Drive, not here" files={status.drive_changes} />
            </>
          )}
          {kind === 'unlinked' && <Files title="differs from Drive" files={status!.local_changes} />}

          <div className="model-actions">
            <button
              type="button"
              className="btn"
              disabled={busy || !status?.can_pull}
              onClick={() => void run('pull')}
            >
              pull from Drive
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || !status?.can_push}
              onClick={() => void run('push')}
            >
              push to Drive
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => void refresh()}>
              refresh
            </button>
          </div>
          {conflicted && (
            <div className="model-actions">
              <button type="button" className="btn" disabled={busy} onClick={() => void run('pull', true)}>
                pull anyway
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => void run('push', true)}>
                push anyway
              </button>
            </div>
          )}
          {busy && job && <Progress job={job} />}
          {note && <p className="small">{note}</p>}
          {error && <p className="error small">{error}</p>}
        </div>
      )}
    </div>
  )
}
