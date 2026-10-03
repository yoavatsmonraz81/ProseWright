/** A working copy of the open scene. The log is not touched until the
 *  author proposes and accepts the diff in the fork pane. */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { PageMessage } from '../types'
import { withEmDash } from '../lib/emDash'

interface Draft {
  msgId: number
  speaker: string
  body: string
  original: string
}

interface Props {
  messages: PageMessage[]
  focusMessage: number | null
  busy: boolean
  error: string | null
  onPropose: (patches: { msg_id: number; body: string }[]) => void
  onCancel: () => void
}

export function LogEditPanel({
  messages,
  focusMessage,
  busy,
  error,
  onPropose,
  onCancel,
}: Props) {
  const [drafts, setDrafts] = useState<Draft[]>(() => snapshot(messages))
  const focusRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    focusRef.current?.focus()
    focusRef.current?.scrollIntoView({ block: 'center' })
  }, [])

  const dirty = useMemo(
    () => drafts.filter((d) => d.body !== d.original),
    [drafts],
  )
  const span = messages.length
    ? `msgs ${messages[0].msg_id}–${messages[messages.length - 1].msg_id}`
    : 'empty scene'

  const propose = () => {
    if (!dirty.length || busy) return
    onPropose(dirty.map((d) => ({ msg_id: d.msgId, body: d.body })))
  }

  return (
    <div className="log-edit">
      <header className="log-edit-head">
        <p>
          Working copy of {span}. The log is unchanged until you propose and
          accept.
        </p>
        <span className="muted small">
          {dirty.length === 0
            ? 'nothing changed yet'
            : `${dirty.length} turn${dirty.length === 1 ? '' : 's'} changed`}
        </span>
        <div className="row">
          <button className="chip ghost" disabled={busy} onClick={onCancel}>
            discard copy
          </button>
          <button
            className="stamp-button"
            disabled={busy || dirty.length === 0}
            onClick={propose}
          >
            propose these edits
          </button>
        </div>
      </header>

      <div className="log-edit-list">
        {drafts.map((draft) => {
          const changed = draft.body !== draft.original
          return (
            <section
              key={draft.msgId}
              className={`log-edit-card ${changed ? 'is-dirty' : ''}`}
            >
              <h4>
                msg {draft.msgId} · {draft.speaker}
                {changed ? ' · changed' : ''}
              </h4>
              <textarea
                ref={draft.msgId === focusMessage ? focusRef : undefined}
                rows={Math.min(18, Math.max(4, draft.body.split('\n').length + 1))}
                value={draft.body}
                disabled={busy}
                onChange={(e) => {
                  const value = withEmDash(e.target)
                  setDrafts((prev) =>
                    prev.map((d) =>
                      d.msgId === draft.msgId ? { ...d, body: value } : d,
                    ),
                  )
                }}
              />
            </section>
          )
        })}
      </div>

      {error && <p className="error">{error}</p>}
    </div>
  )
}

function snapshot(messages: PageMessage[]): Draft[] {
  return messages.map((m) => {
    const body = splitHeader(m.text)
    return {
      msgId: m.msg_id,
      speaker: m.speaker,
      body,
      original: body,
    }
  })
}

/** Drop a leading `[ … ]` status header so the copy is the prose. The header
 *  is put back from the log when the proposal is built. */
function splitHeader(text: string): string {
  const stripped = text.replace(/^\s+/, '')
  if (!stripped.startsWith('[')) return text
  const end = stripped.indexOf(']')
  if (end < 0 || end > 400) return text
  return stripped.slice(end + 1).replace(/^[ \t\r\n]+/, '')
}
