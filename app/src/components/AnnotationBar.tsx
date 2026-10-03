/** The floating bar a selection raises.
 *
 * The recording action, who spoke, writes to a sidecar the engine already owns
 * (voice attribution), so nothing invented for this pane has to be kept in step
 * with the rest of the engine later.
 *
 * `propose…` sits beside them because a selection is the trigger for both halves
 * of the loop: one says something about these turns, the other changes them. It
 * leads somewhere else, though — a composer, then a diff — and nothing it starts
 * touches the log without a verdict.
 */

import { useState } from 'react'

interface Props {
  range: { from: number; to: number }
  busy: boolean
  error: string | null
  onVoice: (voice: string, mode: string) => void
  /** Card names inside the selection — used to pick pov vs wrong-card. */
  speakers?: string[]
  onPropose: () => void
  onRemove: (sweep: boolean) => void
  onDismiss: () => void
  /** Voices known in this log (from the page filter's catalogue). */
  cast?: string[]
}

export function AnnotationBar({
  range,
  busy,
  error,
  onVoice,
  speakers = [],
  onPropose,
  onRemove,
  onDismiss,
  cast = [],
}: Props) {
  const voices = Array.from(new Set([...speakers.map(cardVoice), ...cast, 'ensemble']))
  const [mode, setMode] = useState<'none' | 'voice'>('none')

  const span =
    range.from === range.to ? `msg ${range.from}` : `msgs ${range.from}\u2013${range.to}`

  return (
    <div className="annobar" role="toolbar">
      <span className="annobar-span">{span}</span>

      <button
        className={`chip ${mode === 'voice' ? 'is-on' : ''}`}
        disabled={busy}
        onClick={() => setMode(mode === 'voice' ? 'none' : 'voice')}
        title="who was really speaking in these turns"
      >
        who spoke
      </button>

      <button
        className="chip propose"
        disabled={busy}
        onClick={onPropose}
        title="rewrite these turns — proposed first, never written straight in"
      >
        propose…
      </button>
      <button
        className="chip no"
        disabled={busy}
        onClick={() => onRemove(false)}
        title="take these turns out of the log — proposed first, never written straight in"
      >
        remove
      </button>
      <button
        className="chip no"
        disabled={busy}
        onClick={() => onRemove(true)}
        title="take these turns out, then look downstream for anything that leaned on them"
      >
        remove + sweep
      </button>


      <button className="chip ghost" onClick={onDismiss} title="clear selection (esc)">
        ✕
      </button>

      {mode === 'voice' && (
        <div className="annobar-drawer">
          {voices.map((v) => (
            <button
              key={v}
              className="chip"
              disabled={busy}
              title={
                v === 'ensemble'
                  ? 'several people in one turn, no single mouth'
                  : 'who was really voiced — writes the label on the slug'
              }
              onClick={() => onVoice(v, modeFor(v, speakers))}
            >
              {v}
            </button>
          ))}
        </div>
      )}

      {error && <span className="error small">{error}</span>}
    </div>
  )
}

function cardVoice(speaker: string): string {
  return speaker.trim().toLowerCase().replace(/-/g, '_').replace(/ /g, '_')
}

function modeFor(voice: string, speakers: string[]): string {
  if (voice === 'ensemble' || voice === 'narrator') return 'scene_narrator'
  if (speakers.length > 0 && speakers.every((s) => cardVoice(s) === voice)) return 'pov'
  if (speakers.length > 0) return 'wrong_card'
  return 'pov'
}
