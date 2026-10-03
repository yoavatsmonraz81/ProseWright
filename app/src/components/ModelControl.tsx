/** Live model endpoint switch — local textgen ↔ OpenRouter.
 *
 * Lives in the status bar because the rest of the app already reports
 * reachability there. Changing provider mutates the running engine only; it
 * does not rewrite shell env. The API key is never echoed back.
 */

import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { EngineStatus } from '../types'

interface Props {
  status: EngineStatus | null
  onChanged: (next: EngineStatus) => void
}

function shortModel(name: string | undefined): string {
  if (!name) return '—'
  const base = name.split('/').pop() ?? name
  return base.length > 28 ? `${base.slice(0, 26)}…` : base
}

export function ModelControl({ status, onChanged }: Props) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [model, setModel] = useState('')
  const [localPort, setLocalPort] = useState('')
  const [apiKey, setApiKey] = useState('')
  const panelRef = useRef<HTMLDivElement>(null)

  const provider = status?.model_provider ?? status?.model?.provider ?? 'local'
  const modelName = status?.model_default ?? status?.model?.model ?? ''
  const keySet = status?.model_api_key_set ?? status?.model?.api_key_set ?? false
  const reachable = status?.model_reachable ?? false

  useEffect(() => {
    if (open) {
      setModel(modelName)
      setLocalPort(String(status?.model?.local_port ?? 5000))
      setApiKey('')
      setError(null)
    }
  }, [open, modelName, status?.model?.local_port])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  async function apply(next: {
    provider?: string
    model?: string
    api_key?: string
    local_port?: number
  }) {
    setBusy(true)
    setError(null)
    try {
      const body: {
        provider?: string
        model?: string
        api_key?: string
        local_port?: number
      } = {}
      if (next.provider) body.provider = next.provider
      if (next.model?.trim()) body.model = next.model.trim()
      if (next.api_key?.trim()) body.api_key = next.api_key.trim()
      if (next.local_port !== undefined) body.local_port = next.local_port
      await api.setModel(body)
      const fresh = await api.status()
      onChanged(fresh)
      setApiKey('')
      if (next.provider && next.provider !== provider) setOpen(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function pickProvider(next: 'local' | 'openrouter') {
    if (next === provider || busy) return
    if (next === 'openrouter' && !keySet) {
      setOpen(true)
      setError('OpenRouter needs an API key — paste it below, then switch.')
      return
    }
    await apply({ provider: next })
  }

  const defaults = status?.model?.defaults

  return (
    <div className="model-control" ref={panelRef}>
      <div className="model-seg" role="group" aria-label="Model provider">
        <button
          type="button"
          className={provider === 'local' ? 'is-on' : ''}
          disabled={busy}
          onClick={() => void pickProvider('local')}
          title={defaults?.local.base_url}
        >
          local
        </button>
        <button
          type="button"
          className={provider === 'openrouter' ? 'is-on' : ''}
          disabled={busy}
          onClick={() => void pickProvider('openrouter')}
          title={defaults?.openrouter.base_url}
        >
          openrouter
        </button>
      </div>
      <button
        type="button"
        className={`model-chip ${reachable ? 'ok' : 'warn'}`}
        onClick={() => setOpen((v) => !v)}
        title={modelName}
      >
        {shortModel(modelName)}
        {provider === 'local' && status?.model?.local_port
          ? ` · :${status.model.local_port}`
          : ''}
        {!keySet && provider === 'openrouter' ? ' · no key' : ''}
        {reachable ? '' : ' · down'}
      </button>

      {open && (
        <div className="model-panel">
          <header>
            <h4>Model endpoint</h4>
            <span className="muted">{provider}</span>
          </header>
          <label>
            <span>model id</span>
            <input
              value={model}
              disabled={busy}
              placeholder={
                provider === 'openrouter'
                  ? defaults?.openrouter.model
                  : defaults?.local.model
              }
              onChange={(e) => setModel(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void apply({ model })
              }}
            />
          </label>
          {provider === 'local' && (
            <label>
              <span>local API port</span>
              <input
                type="number"
                min={1}
                max={65535}
                value={localPort}
                disabled={busy}
                placeholder="5000"
                onChange={(e) => setLocalPort(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    const port = Number(localPort)
                    if (Number.isInteger(port) && port >= 1 && port <= 65535) {
                      void apply({ model, local_port: port })
                    } else {
                      setError('Port must be a whole number from 1 to 65535.')
                    }
                  }
                }}
              />
            </label>
          )}
          <label>
            <span>
              api key
              {keySet ? ' (set — leave blank to keep)' : ' (required for openrouter)'}
            </span>
            <input
              type="password"
              value={apiKey}
              disabled={busy}
              autoComplete="off"
              placeholder={keySet ? '••••••••' : 'sk-or-…'}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <div className="model-actions">
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() =>
                (() => {
                  const port = Number(localPort)
                  if (
                    provider === 'local'
                    && (!Number.isInteger(port) || port < 1 || port > 65535)
                  ) {
                    setError('Port must be a whole number from 1 to 65535.')
                    return
                  }
                  void apply({
                    model,
                    ...(provider === 'local' ? { local_port: port } : {}),
                    ...(apiKey.trim() ? { api_key: apiKey } : {}),
                  })
                })()
              }
            >
              {busy ? 'saving…' : 'apply'}
            </button>
            {provider !== 'openrouter' && (
              <button
                type="button"
                className="btn"
                disabled={busy || (!keySet && !apiKey.trim())}
                onClick={() =>
                  void apply({
                    provider: 'openrouter',
                    model: model || defaults?.openrouter.model,
                    ...(apiKey.trim() ? { api_key: apiKey } : {}),
                  })
                }
              >
                switch to openrouter
              </button>
            )}
            {provider !== 'local' && (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => void apply({ provider: 'local' })}
              >
                switch to local
              </button>
            )}
          </div>
          {error && <p className="error">{error}</p>}
          <p className="muted small">
            Runtime only. For restart defaults use STORY_EDITOR_MODEL_PROVIDER and
            STORY_EDITOR_MODEL_PORT.
          </p>
        </div>
      )}
    </div>
  )
}
