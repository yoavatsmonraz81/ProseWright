/** The page: a scene's turns, decorated, with marks in the gutter.
 *
 * Read-only on the log layer, and that is a rule rather than an omission. Log
 * prose is rewritten through the transform operators, which keep a reviewable
 * edit set and a backup; a text box that writes straight into the roleplay
 * master would route around both. Hand editing arrives with the manuscript
 * layer, where the engine's own layer rules allow it.
 */

import { useEffect, useMemo, useRef } from 'react'
import { EditorState, StateField } from '@codemirror/state'
import {
  Decoration,
  EditorView,
  GutterMarker,
  gutter,
  type DecorationSet,
} from '@codemirror/view'
import { buildPageDoc, lineOfMessage, type PageDoc } from '../lib/page'
import type { PageMessage } from '../types'

interface Props {
  messages: PageMessage[]
  selection: { from: number; to: number } | null
  focusMessage: number | null
  onSelect: (range: { from: number; to: number } | null) => void
  fontFamily: string
  fontSize: number
  lineHeight: number
  measure: number
  tracking: number
}

const slugLine = Decoration.line({ class: 'cm-slug' })
const emphasis = Decoration.mark({ class: 'cm-em' })
const strong = Decoration.mark({ class: 'cm-strong' })
const hidden = Decoration.replace({})

/** The log writes emphasis in markdown, and the page is a manuscript: showing
 *  `**asterisks**` would be showing the machinery. Render the emphasis and hide
 *  its markers — the text is read-only, so nothing can be typed into the gap. */
const EMPHASIS = /(\*\*|\*)(?=\S)([\s\S]*?\S)\1/g

function emphasisRanges(text: string, offset: number) {
  const out: { from: number; to: number; deco: Decoration }[] = []
  EMPHASIS.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = EMPHASIS.exec(text)) !== null) {
    const [whole, marker, inner] = match
    const start = offset + match.index
    const innerFrom = start + marker.length
    const innerTo = innerFrom + inner.length
    out.push({ from: start, to: innerFrom, deco: hidden })
    out.push({
      from: innerFrom,
      to: innerTo,
      deco: marker === '**' ? strong : emphasis,
    })
    out.push({ from: innerTo, to: start + whole.length, deco: hidden })
  }
  return out
}

const interludeLine = Decoration.line({ class: 'cm-interlude' })
const selectedLine = Decoration.line({ class: 'cm-turn-selected' })

class MarkGutter extends GutterMarker {
  label: string
  cls: string
  title: string

  constructor(label: string, cls: string, title: string) {
    super()
    this.label = label
    this.cls = cls
    this.title = title
  }

  eq(other: MarkGutter) {
    return other.label === this.label && other.cls === this.cls
  }
  toDOM() {
    const span = document.createElement('span')
    span.className = `cm-mark ${this.cls}`
    span.textContent = this.label
    span.title = this.title
    return span
  }
}

export function PageView({
  messages,
  selection,
  focusMessage,
  onSelect,
  fontFamily,
  fontSize,
  lineHeight,
  measure,
  tracking,
}: Props) {
  const host = useRef<HTMLDivElement | null>(null)
  const view = useRef<EditorView | null>(null)
  const doc = useMemo(() => buildPageDoc(messages), [messages])
  const docRef = useRef<PageDoc>(doc)
  const onSelectRef = useRef(onSelect)
  const selectionRef = useRef(selection)

  docRef.current = doc
  onSelectRef.current = onSelect
  selectionRef.current = selection

  // One state per scene. Rebuilding is cheaper than threading effects through a
  // dozen fields, and a scene is at most a few dozen turns.
  useEffect(() => {
    if (!host.current) return

    const byId = new Map(messages.map((m) => [m.msg_id, m]))

    const decorations = StateField.define<DecorationSet>({
      create: (state) => build(state),
      update: (value, tr) => (tr.docChanged || tr.selection ? build(tr.state) : value),
      provide: (f) => EditorView.decorations.from(f),
    })

    function build(state: EditorState): DecorationSet {
      // Collected then sorted rather than streamed into a RangeSetBuilder: line
      // and inline decorations interleave, and Decoration.set sorts them into
      // the order the builder would otherwise demand up front.
      const ranges: { from: number; to: number; deco: Decoration }[] = []
      const sel = selectionRef.current
      const current = docRef.current
      for (const info of current.lines) {
        if (info.line > state.doc.lines) break
        const line = state.doc.line(info.line)
        const message = byId.get(info.msgId)
        if (!message) continue
        if (sel && message.msg_id >= sel.from && message.msg_id <= sel.to) {
          ranges.push({ from: line.from, to: line.from, deco: selectedLine })
        }
        if (message.interlude) {
          ranges.push({ from: line.from, to: line.from, deco: interludeLine })
        }
        if (info.kind === 'slug') {
          ranges.push({ from: line.from, to: line.from, deco: slugLine })
          continue
        }
        if (info.kind === 'prose') {
          ranges.push(...emphasisRanges(line.text, line.from))
        }
      }
      return Decoration.set(
        ranges.map((r) => r.deco.range(r.from, r.to)),
        true,
      )
    }

    const marks = gutter({
      class: 'cm-gutter-marks',
      lineMarker: (v, line) => {
        const info = docRef.current.lines[v.state.doc.lineAt(line.from).number - 1]
        if (!info || info.kind !== 'slug') return null
        const message = byId.get(info.msgId)
        if (!message) return null
        if (message.manuscript_scene) {
          return new MarkGutter('\u270e', 'cm-mark-novel', 'novelized')
        }
        return null
      },
      initialSpacer: () => new MarkGutter('\u270e', 'cm-mark-novel', ''),
    })

    const ids = gutter({
      class: 'cm-gutter-ids',
      lineMarker: (v, line) => {
        const info = docRef.current.lines[v.state.doc.lineAt(line.from).number - 1]
        if (!info || info.kind !== 'slug') return null
        return new MarkGutter(String(info.msgId), 'cm-mark-id', `message ${info.msgId}`)
      },
      initialSpacer: () => new MarkGutter('000', 'cm-mark-id', ''),
    })

    const state = EditorState.create({
      doc: doc.text,
      extensions: [
        EditorView.editable.of(false),
        EditorState.readOnly.of(true),
        EditorView.lineWrapping,
        decorations,
        ids,
        marks,
        EditorView.updateListener.of((update) => {
          if (!update.selectionSet) return
          const range = update.state.selection.main
          const current = docRef.current
          const fromLine = update.state.doc.lineAt(range.from).number
          const toLine = update.state.doc.lineAt(range.to).number
          const first = current.lines[fromLine - 1]
          const last = current.lines[toLine - 1]
          if (!first || !last) return
          if (range.empty) {
            onSelectRef.current(null)
            return
          }
          onSelectRef.current({
            from: Math.min(first.msgId, last.msgId),
            to: Math.max(first.msgId, last.msgId),
          })
        }),
      ],
    })

    view.current?.destroy()
    view.current = new EditorView({ state, parent: host.current })
    return () => {
      view.current?.destroy()
      view.current = null
    }
  }, [doc, messages])

  // Redraw decorations when the selection changes outside the editor (a keyboard
  // command, a click in the rail) — the field reads selectionRef, so a no-op
  // transaction is enough to make it recompute.
  useEffect(() => {
    view.current?.dispatch({})
  }, [selection])

  useEffect(() => {
    const v = view.current
    if (!v || focusMessage === null) return
    const line = lineOfMessage(doc, focusMessage)
    if (line === null || line > v.state.doc.lines) return
    const pos = v.state.doc.line(line).from
    v.dispatch({ effects: EditorView.scrollIntoView(pos, { y: 'start', yMargin: 24 }) })
  }, [focusMessage, doc])

  return (
    <div
      className="page"
      ref={host}
      style={
        {
          '--page-font': fontFamily,
          '--page-size': `${fontSize}px`,
          '--page-leading': String(lineHeight),
          '--page-measure': `${measure}ch`,
          '--page-tracking': `${tracking}em`,
        } as React.CSSProperties
      }
    />
  )
}
