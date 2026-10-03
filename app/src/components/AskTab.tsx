/** Ask: grounded Q&A over the log, with an optional story-time ceiling.

 * Retrieval already lives in the engine. This pane is the face — ask a question,
 * optionally cut the log at ``as_of`` (msg / derived beat / authored beat), and
 * jump the page from cited messages.
 */

import { useEffect, useState } from 'react'
import type { AskOptions, AskResult } from '../types'

interface Props {
  options: AskOptions | null
  result: AskResult | null
  busy: boolean
  phase: string
  error: string | null
  onAsk: (question: string, asOf: string, mode: 'fused' | 'keyword') => void
  onCite: (msgId: number) => void
}

export function AskTab({
  options,
  result,
  busy,
  phase,
  error,
  onAsk,
  onCite,
}: Props) {
  const [question, setQuestion] = useState('')
  const [asOf, setAsOf] = useState('')
  const [mode, setMode] = useState<'fused' | 'keyword'>('fused')

  useEffect(() => {
    // Keep a custom as_of the user typed; only clear when options reload empties tip.
  }, [options])

  const submit = () => {
    const q = question.trim()
    if (!q || busy) return
    onAsk(q, asOf.trim(), mode)
  }

  return (
    <div className="rail-body ask-tab">
      <p className="stamp-note">
        Answers from the indexed log only. Set <em>as of</em> to ignore later
        prose — same ceiling dossiers will use.
      </p>

      <label className="ask-field">
        <span className="ask-label">question</span>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          rows={4}
          placeholder="What does Ilse know about the Gull's Due?"
          disabled={busy}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault()
              submit()
            }
          }}
        />
      </label>

      <label className="ask-field">
        <span className="ask-label">as of</span>
        <div className="ask-asof-row">
          <input
            type="text"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            placeholder="now · msg 504 · D8 · A6"
            disabled={busy}
            list="ask-asof-options"
          />
          <datalist id="ask-asof-options">
            <option value="" label="full log (now)" />
            {(options?.derived ?? []).map((o) => (
              <option key={o.value} value={o.value} label={o.label} />
            ))}
            {(options?.authored ?? []).map((o) => (
              <option key={o.value} value={o.value} label={o.label} />
            ))}
          </datalist>
        </div>
        {options && (
          <span className="stamp-note">
            log tip msg {options.log_tip}
            {asOf ? ` · ceiling ${asOf}` : ' · no ceiling'}
          </span>
        )}
      </label>

      <div className="ask-modes" role="group" aria-label="search mode">
        <button
          type="button"
          className={`chip ${mode === 'fused' ? 'is-on' : 'ghost'}`}
          onClick={() => setMode('fused')}
          disabled={busy}
        >
          fused
        </button>
        <button
          type="button"
          className={`chip ${mode === 'keyword' ? 'is-on' : 'ghost'}`}
          onClick={() => setMode('keyword')}
          disabled={busy}
        >
          keyword
        </button>
        <button
          type="button"
          className="btn"
          onClick={submit}
          disabled={busy || !question.trim()}
        >
          {busy ? 'asking…' : 'ask'}
        </button>
      </div>

      {busy && (
        <p className="stamp-note validate-progress-label" aria-live="polite">
          {phase || 'asking…'}
        </p>
      )}
      {error && <p className="error">{error}</p>}

      {result && (
        <div className="ask-result">
          {result.as_of && (
            <p className="stamp-note">as of {result.as_of.label}</p>
          )}
          <div className="ask-answer">{result.answer}</div>
          {result.citations.length > 0 && (
            <>
              <h4 className="rail-sub">citations</h4>
              <ul className="ask-citations">
                {result.citations.map((c) => (
                  <li key={c.msg_id}>
                    <button
                      type="button"
                      className="ask-cite"
                      onClick={() => onCite(c.msg_id)}
                      title={`jump to msg ${c.msg_id}`}
                    >
                      <span className="ask-cite-id">
                        msg {c.msg_id}
                        {c.speaker ? ` · ${c.speaker}` : ''}
                      </span>
                      <span className="ask-cite-preview">{c.preview}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="stamp-note">
            {result.hits_considered} passages considered · {result.mode}
          </p>
        </div>
      )}
    </div>
  )
}
