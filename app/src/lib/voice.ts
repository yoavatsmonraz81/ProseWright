/** Whose single voice a log turn is — the page filter's idea of "who speaks".

 * Mixed and scene-narrator turns have no single voice, and neither do
 * interludes. A label (reviewed or proposed) wins; otherwise the card's name.
 */

import type { PageMessage } from '../types'

const SKIP_MODES = new Set(['scene_narrator', 'mixed'])

function cardKey(speaker: string): string {
  return speaker.trim().toLowerCase().replace(/-/g, '_').replace(/ /g, '_')
}

export function mouthOf(m: PageMessage): string | null {
  if (m.interlude) return null
  const mode = (m.mode || '').toLowerCase()
  if (SKIP_MODES.has(mode)) return null
  const labeled = (m.voice || '').toLowerCase()
  if (labeled) {
    if (labeled === 'unknown' || labeled === 'ensemble') return null
    return labeled
  }
  return cardKey(m.speaker) || null
}

export function isVoiced(m: PageMessage, voice: string): boolean {
  return mouthOf(m) === voice
}

export function voiceLabel(voice: string): string {
  if (!voice) return voice
  return voice.charAt(0).toUpperCase() + voice.slice(1).replace(/_/g, ' ')
}
