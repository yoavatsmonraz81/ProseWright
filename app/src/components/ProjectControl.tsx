/** Which story is open — and switching to another, or saving this one.
 *
 * Projects live in the registry (python -m story_editor project …). Switching
 * makes another project active and restarts the engine on it; the page reloads
 * once the engine answers again. Saving downloads a .sebundle of the open
 * project. Loading one uploads the file, shows what's in it, and restores it as
 * a new project in the projects folder — it never replaces an existing one.
 */

import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { LoadedProject, ProjectsView, StagedUpload } from '../types'

async function waitForProject(id: string): Promise<void> {
  // The old process answers until it execs; give it a moment, then poll.
  await new Promise((r) => window.setTimeout(r, 1500))
  for (let i = 0; i < 60; i++) {
    try {
      const view = await api.projects()
      if (view.current.id === id) return
    } catch {
      // restarting
    }
    await new Promise((r) => window.setTimeout(r, 1000))
  }
}

export function ProjectControl() {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<ProjectsView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [adding, setAdding] = useState('')
  const [withBackups, setWithBackups] = useState(false)
  const [staged, setStaged] = useState<StagedUpload | null>(null)
  const [loadId, setLoadId] = useState('')
  const [loaded, setLoaded] = useState<LoadedProject | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const refresh = async () => {
    try {
      setView(await api.projects())
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

  async function run(work: () => Promise<void>) {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await work()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const switchTo = (id: string, title: string) =>
    run(async () => {
      await api.switchProject(id)
      setNote(`Opening ${title || id} — restarting the engine…`)
      await waitForProject(id)
      window.location.reload()
    })

  const add = () =>
    run(async () => {
      setView(await api.addProject(adding.trim()))
      setAdding('')
    })

  const save = () =>
    run(async () => {
      await api.saveProjectBundle({ with_backups: withBackups })
      setNote('Bundle saved to your downloads. Restore it with `python -m story_editor project load FILE`.')
    })

  const pick = (file: File | undefined) => {
    if (!file) return
    void run(async () => {
      setLoaded(null)
      setNote(`Checking ${file.name}…`)
      const result = await api.uploadBundle(file)
      setStaged(result)
      setLoadId(result.suggested_id)
      setNote(null)
    })
  }

  const confirmLoad = () =>
    run(async () => {
      if (!staged) return
      const result = await api.loadBundle(staged.token, loadId.trim())
      setStaged(null)
      setLoaded(result.loaded)
      setView(result)
    })

  const cancelLoad = () =>
    run(async () => {
      if (staged) await api.discardUpload(staged.token)
      setStaged(null)
    })

  const mb = (bytes: number) =>
    bytes < 1_000_000 ? `${Math.max(1, Math.round(bytes / 1000))} KB` : `${(bytes / 1_000_000).toFixed(1)} MB`
  const current = view?.current
  const tone = current?.warning || view?.registry_error ? 'warn' : 'ok'

  return (
    <div className="model-control drive-control" ref={panelRef}>
      <button type="button" className={`model-chip ${tone}`} onClick={() => setOpen((v) => !v)}
        title={current?.home}>
        project · {current ? current.title || current.id : '…'}
      </button>

      {open && view && (
        <div className="model-panel drive-panel project-panel">
          <header>
            <h4>Projects</h4>
            <span className="muted">{current?.id}</span>
          </header>
          <p className="small muted">{current?.home}</p>
          {current?.warning && <p className="small">{current.warning}</p>}
          {view.registry_error && <p className="small">{view.registry_error}</p>}
          {view.env_override && (
            <p className="small">
              Started with <code>STORY_EDITOR_HOME</code>; switching restarts the engine without it.
            </p>
          )}

          {view.projects.length > 0 ? (
            <ul className="project-list">
              {view.projects.map((p) => (
                <li key={p.id}>
                  <span title={p.home}>
                    <strong>{p.title || p.id}</strong> <span className="muted small">{p.id}</span>
                    {!p.exists && <span className="small"> · folder missing</span>}
                  </span>
                  {p.open ? (
                    <span className="muted small">open</span>
                  ) : (
                    <button type="button" className="btn" disabled={busy || !p.exists}
                      onClick={() => void switchTo(p.id, p.title)}>
                      open
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="small">
              No projects registered yet. Add a project folder below, or from the shell:
              <code> python -m story_editor project add PATH</code>
            </p>
          )}

          <div className="model-actions">
            <input
              className="text-input"
              placeholder="/path/to/a/project folder"
              value={adding}
              onChange={(e) => setAdding(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && adding.trim()) void add()
              }}
            />
            <button type="button" className="btn" disabled={busy || !adding.trim()} onClick={() => void add()}>
              add
            </button>
          </div>

          <div className="model-actions">
            <button type="button" className="btn" disabled={busy} onClick={() => void save()}
              title="download a .sebundle of this project (checksummed; loads into a new folder)">
              save bundle
            </button>
            <button type="button" className="btn" disabled={busy || !!staged}
              onClick={() => fileRef.current?.click()}
              title="restore a .sebundle as a new project (never replaces an existing one)">
              load bundle…
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".sebundle"
              hidden
              onChange={(e) => {
                pick(e.target.files?.[0])
                e.target.value = ''
              }}
            />
            <label className="small inline-check">
              <input type="checkbox" checked={withBackups} onChange={(e) => setWithBackups(e.target.checked)} />
              include backups
            </label>
          </div>

          {staged && (
            <div className="project-load">
              <p className="small">
                <strong>{staged.bundle.title || staged.bundle.project_id}</strong> ·{' '}
                {staged.bundle.files} files, {mb(staged.bundle.bytes)} · checked
                {staged.bundle.created ? ` · saved ${staged.bundle.created.slice(0, 16).replace('T', ' ')}` : ''}
                {staged.bundle.machine ? ` on ${staged.bundle.machine}` : ''}
              </p>
              {staged.bundle.external.length > 0 && (
                <p className="small muted">
                  Carries private copies of {staged.bundle.external.length} file(s) from outside its folder
                  (chat, lorebooks, notes); the copy uses those, never the originals.
                </p>
              )}
              <div className="model-actions">
                <label className="small">load as</label>
                <input
                  className="text-input"
                  value={loadId}
                  onChange={(e) => setLoadId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && loadId.trim()) void confirmLoad()
                  }}
                />
              </div>
              <p className="small muted">into {staged.projects_dir}/{loadId.trim() || '…'}</p>
              <div className="model-actions">
                <button type="button" className="btn" disabled={busy || !loadId.trim()}
                  onClick={() => void confirmLoad()}>
                  load
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => void cancelLoad()}>
                  cancel
                </button>
              </div>
            </div>
          )}

          {loaded && (
            <div className="project-load">
              <p className="small">
                Loaded <strong>{loaded.title}</strong> as <code>{loaded.project_id}</code> into {loaded.home}{' '}
                ({loaded.files} files, all checked).
              </p>
              {loaded.rewrites.length > 0 && (
                <ul className="small muted">
                  {loaded.rewrites.map((r) => <li key={r}>{r}</li>)}
                </ul>
              )}
              <div className="model-actions">
                <button type="button" className="btn" disabled={busy}
                  onClick={() => void switchTo(loaded.project_id, loaded.title)}>
                  open now
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => setLoaded(null)}>
                  done
                </button>
              </div>
            </div>
          )}

          {note && <p className="small">{note}</p>}
          {error && <p className="error small">{error}</p>}
        </div>
      )}
    </div>
  )
}
