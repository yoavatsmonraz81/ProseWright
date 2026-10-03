/** Turning a scene's turns into a page, and a selection back into turns.
 *
 * The page is one CodeMirror document per scene. Each turn contributes a slug
 * line (speaker, id, marks) and its prose, and a line→message map is built
 * alongside so every later question — which turn is under the cursor, which
 * turns does this selection cover, where do the gutter marks go — is a lookup
 * rather than a guess.
 *
 * Selections snap to whole turns. That is a deliberate limit: a turn has a uid,
 * so an annotation on it survives restyling and injection, while a character
 * offset into prose the model may rewrite does not. Sub-turn marks would need
 * their own anchoring story, and Phase 0 exists precisely because anchoring
 * stories that look cheap are not.
 */

import type { PageMessage } from '../types'

export interface LineInfo {
  /** 1-based CodeMirror line number. */
  line: number
  msgId: number
  kind: 'slug' | 'prose' | 'blank'
}

export interface PageDoc {
  text: string
  lines: LineInfo[]
  /** msgId → the lines it owns, in order. */
  byMessage: Map<number, LineInfo[]>
  messages: PageMessage[]
}

export const SLUG_PREFIX = '\u2500\u2500 '

function slugFor(m: PageMessage): string {
  const bits: string[] = [`${m.speaker}`]
  if (m.voice && m.voice !== m.speaker.toLowerCase()) bits.push(`voiced ${m.voice}`)
  if (m.mode && m.mode !== 'pov') bits.push(m.mode.replace(/_/g, ' '))
  if (m.interlude) bits.push('interlude')
  return `${SLUG_PREFIX}${bits.join(' \u00b7 ')}`
}

export function buildPageDoc(messages: PageMessage[]): PageDoc {
  const out: string[] = []
  const lines: LineInfo[] = []
  const byMessage = new Map<number, LineInfo[]>()

  const push = (text: string, msgId: number, kind: LineInfo['kind']) => {
    out.push(text)
    const info: LineInfo = { line: out.length, msgId, kind }
    lines.push(info)
    const bucket = byMessage.get(msgId)
    if (bucket) bucket.push(info)
    else byMessage.set(msgId, [info])
  }

  messages.forEach((m, index) => {
    if (index > 0) push('', m.msg_id, 'blank')
    push(slugFor(m), m.msg_id, 'slug')
    const body = m.prose.trim() || '(empty turn)'
    for (const paragraph of body.split(/\n/)) push(paragraph, m.msg_id, 'prose')
  })

  return { text: out.join('\n'), lines, byMessage, messages }
}

export function messageAtLine(doc: PageDoc, line: number): number | null {
  const info = doc.lines[line - 1]
  return info ? info.msgId : null
}

/** The turn span a character range touches, snapped outward to whole turns. */
export function messageRange(
  doc: PageDoc,
  fromLine: number,
  toLine: number,
): { from: number; to: number } | null {
  const first = messageAtLine(doc, Math.min(fromLine, toLine))
  const last = messageAtLine(doc, Math.max(fromLine, toLine))
  if (first === null || last === null) return null
  return { from: Math.min(first, last), to: Math.max(first, last) }
}

export function lineOfMessage(doc: PageDoc, msgId: number): number | null {
  const bucket = doc.byMessage.get(msgId)
  if (!bucket) return null
  const slug = bucket.find((l) => l.kind === 'slug')
  return (slug ?? bucket[0]).line
}
