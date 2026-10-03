/** ⌘K: jump to a scene, a beat, or a message number.
 *
 * Deliberately only navigation. A palette that also performs operations becomes
 * a second, undiscoverable copy of the UI, and the operations here write to the
 * manuscript.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { DerivedBeat, SceneRow } from '../types'

export interface Jump {
  kind: 'scene' | 'beat' | 'message'
  msgId: number
  sceneId?: number
  label: string
  detail: string
}

interface Props {
  scenes: SceneRow[]
  beats: DerivedBeat[]
  unitType?: 'beat' | 'episode'
  onJump: (jump: Jump) => void
  onClose: () => void
}

export function Palette({ scenes, beats, unitType = 'beat', onJump, onClose }: Props) {
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const input = useRef<HTMLInputElement | null>(null)

  useEffect(() => input.current?.focus(), [])

  const results = useMemo<Jump[]>(() => {
    const q = query.trim().toLowerCase()
    const direct = q.match(/^#?(\d{1,4})$/)
    const hits: Jump[] = []

    if (direct) {
      const msgId = Number(direct[1])
      const scene = scenes.find((s) => s.start <= msgId && msgId <= s.end)
      hits.push({
        kind: 'message',
        msgId,
        sceneId: scene?.scene_id,
        label: `message ${msgId}`,
        detail: scene ? `${scene.location} · scene ${scene.scene_id}` : 'outside any scene',
      })
    }

    for (const beat of beats) {
      const hay = `${beat.beat_id} ${beat.title}`.toLowerCase()
      if (q && !hay.includes(q)) continue
      hits.push({
        kind: 'beat',
        msgId: beat.start_msg_id,
        label:
          unitType === 'episode'
            ? `episode ${beat.beat_id + 1} · ${beat.title}`
            : `beat ${beat.beat_id} · ${beat.title}`,
        detail: beat.span_label,
      })
    }

    for (const scene of scenes) {
      const hay = `${scene.scene_id} ${scene.location} ${scene.synopsis} ${
        scene.date ?? ''
      }`.toLowerCase()
      if (q && !hay.includes(q)) continue
      hits.push({
        kind: 'scene',
        msgId: scene.start,
        sceneId: scene.scene_id,
        label:
          scene.kind === 'frontmatter'
            ? scene.location
            : `${scene.kind === 'interlude' ? 'interlude' : 'scene'} ${scene.scene_id} · ${scene.location}`,
        detail:
          scene.kind === 'frontmatter'
            ? 'front matter · not in the log'
            : `${scene.date ?? ''} · msgs ${scene.start}–${scene.end}`,
      })
    }

    return hits.slice(0, 40)
  }, [query, scenes, beats, unitType])

  useEffect(() => setCursor(0), [query])

  return (
    <div className="palette-scrim" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          ref={input}
          value={query}
          placeholder="scene, beat, or #message"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              setCursor((c) => Math.min(c + 1, results.length - 1))
              e.preventDefault()
            }
            if (e.key === 'ArrowUp') {
              setCursor((c) => Math.max(c - 1, 0))
              e.preventDefault()
            }
            if (e.key === 'Enter' && results[cursor]) onJump(results[cursor])
            if (e.key === 'Escape') onClose()
          }}
        />
        <ul>
          {results.map((r, i) => (
            <li key={`${r.kind}-${r.msgId}-${i}`}>
              <button
                className={i === cursor ? 'is-active' : ''}
                onMouseEnter={() => setCursor(i)}
                onClick={() => onJump(r)}
              >
                <span className={`kind ${r.kind}`}>{r.kind}</span>
                <span className="label">{r.label}</span>
                <span className="detail">{r.detail}</span>
              </button>
            </li>
          ))}
          {results.length === 0 && <li className="muted pad">nothing matches</li>}
        </ul>
      </div>
    </div>
  )
}
