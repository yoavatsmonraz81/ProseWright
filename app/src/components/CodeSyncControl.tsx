/** Editor code vs GitHub — the reliable source for the code.
 *
 * Sits beside the Drive control. Update is fast-forward only and refuses when
 * this checkout has uncommitted or unpublished work; after an update the
 * engine restarts itself and the page reloads once it answers again.
 */

import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { CodeStatus } from '../types'

const LABEL: Record<CodeStatus['kind'], string> = {
  up_to_date: 'up to date',
  behind: 'update available',
  ahead: 'unpublished commits',
  diverged: 'diverged',
  no_repo: 'not a git checkout',
  error: 'check failed',
}

async function waitForEngine(): Promise<void> {
  // The old process answers until it execs; give it a moment, then poll.
  await new Promise((r) => window.setTimeout(r, 1500))
  for (let i = 0; i < 60; i++) {
    try {
      await api.status()
      return
    } catch {
      await new Promise((r) => window.setTimeout(r, 1000))
    }
  }
}

export function CodeSyncControl() {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<CodeStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const refresh = async () => {
    try {
      setStatus(await api.codeStatus())
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  useEffect(() => {
    if (!open) return
    void refresh()
    const onDoc = (e: MouseEvent) => {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  async function restartAndReload(message: string) {
    setNote(message)
    await waitForEngine()
    window.location.reload()
  }

  async function update() {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const result = await api.codeUpdate()
      setStatus(result.status)
      if (!result.updated) {
        setNote('Already up to date.')
      } else if (result.dependencies_changed) {
        setNote(
          'Updated, but the Python dependencies changed, so the engine was not restarted. ' +
            'Stop it, run `pip install -r requirements.txt` in the prosewright env, then start it again.',
        )
      } else if (result.restarting) {
        await restartAndReload(`Updated ${result.changed.length} file(s) — restarting the engine…`)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  async function publish() {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const result = await api.codePush()
      setStatus(result.status)
      setNote('Pushed to GitHub.')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const kind = status?.kind
  const tone = kind === 'up_to_date' ? 'ok' : kind === 'diverged' || kind === 'error' ? 'no' : 'warn'

  return (
    <div className="model-control drive-control" ref={panelRef}>
      <button type="button" className={`model-chip ${tone}`} onClick={() => setOpen((v) => !v)}>
        code · {kind ? LABEL[kind] : '…'}
      </button>

      {open && (
        <div className="model-panel drive-panel">
          <header>
            <h4>Editor code · GitHub</h4>
            <span className="muted">
              {status?.head}
              {status?.remote_head && status.remote_head !== status.head ? ` → ${status.remote_head}` : ''}
            </span>
          </header>
          {status?.detail && <p className="small">{status.detail}</p>}
          {kind === 'error' && /could not read|Authentication|credential/i.test(status?.detail ?? '') && (
            <p className="small">Sign this machine in to GitHub once: <code>gh auth login</code></p>
          )}
          {!!status?.incoming.length && (
            <div className="drive-files">
              <span>incoming ({status.incoming.length})</span>
              <ul>{status.incoming.slice(0, 12).map((s, i) => <li key={i}>{s}</li>)}</ul>
            </div>
          )}
          {!!status?.dirty.length && (
            <div className="drive-files">
              <span>uncommitted here ({status.dirty.length}) — blocks updating</span>
              <ul>{status.dirty.slice(0, 12).map((f) => <li key={f}>{f}</li>)}</ul>
            </div>
          )}
          <div className="model-actions">
            <button type="button" className="btn" disabled={busy || !status?.can_update} onClick={() => void update()}>
              update &amp; restart
            </button>
            {status?.can_push && (
              <button type="button" className="btn" disabled={busy} onClick={() => void publish()}>
                push to GitHub
              </button>
            )}
            <button type="button" className="btn" disabled={busy} onClick={() => void refresh()}>
              check
            </button>
          </div>
          {busy && !note && <p className="muted small">talking to GitHub…</p>}
          {note && <p className="small">{note}</p>}
          {error && <p className="error small">{error}</p>}
        </div>
      )}
    </div>
  )
}
