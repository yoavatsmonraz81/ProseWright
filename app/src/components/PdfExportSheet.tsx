import { useEffect, useMemo, useState } from 'react'
import { api, type ExportChapter } from '../api'

export type ExportFormat = 'pdf' | 'txt' | 'ao3'

interface Props {
  format?: ExportFormat
  onClose: () => void
}

export function PdfExportSheet({ format = 'pdf', onClose }: Props) {
  // The server decides what a chapter is (the locked chapter map, or one per
  // scene), so the list here is always the one the exporter will write.
  const [chapters, setChapters] = useState<ExportChapter[] | null>(null)
  const [selected, setSelected] = useState<Set<number>>(() => new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api.exportChapters().then(
      ({ chapters: list }) => {
        if (!live) return
        setChapters(list)
        setSelected(new Set(list.filter((c) => c.has_text).map((c) => c.id)))
      },
      (reason) => live && setError(reason instanceof Error ? reason.message : String(reason)),
    )
    return () => {
      live = false
    }
  }, [])
  const list = chapters ?? []
  const ready = list.filter((c) => c.has_text)
  const allSelected = selected.size === ready.length
  const label = useMemo(
    () => (chapters === null ? 'reading the chapters…' : `${selected.size} of ${list.length} chapters`),
    [chapters, selected.size, list.length],
  )

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runExport = async () => {
    setBusy(true)
    setError(null)
    try {
      const ids = [...selected]
      await (format === 'txt' ? api.exportTxt(ids) : format === 'ao3' ? api.exportAo3(ids) : api.exportPdf(ids))
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="sheet-scrim" onClick={onClose}>
      <section className="sheet pdf-export" onClick={(event) => event.stopPropagation()}>
        <header>
          <h3>{format === 'txt' ? 'Export to text' : format === 'ao3' ? 'Export for AO3' : 'Export to PDF'}</h3>
          <button className="chip ghost" onClick={onClose}>close</button>
        </header>
        <p className="muted small">
          {format === 'ao3'
            ? 'Choose the chapters to include. Each chapter becomes an HTML file for AO3’s HTML editor: paragraphs, italics, and a rule between scenes; the chapter title goes in AO3’s own title field. Several chapters download as one zip.'
            : format === 'txt'
            ? 'Choose the chapters to include. Each chapter becomes its own plain-text file from the Novel layer’s current manuscript; several chapters download as one zip.'
            : 'Choose the chapters to include. The PDF uses the Novel layer’s current manuscript, typeface, paper colour, and interlude artwork.'}
        </p>
        <div className="pdf-export-actions">
          <span className="muted small">{label}</span>
          <button
            className="chip ghost tiny"
            onClick={() => setSelected(allSelected ? new Set() : new Set(ready.map((c) => c.id)))}
          >
            {allSelected ? 'select none' : 'select all'}
          </button>
        </div>
        <ol className="pdf-chapters">
          {list.map((chapter, n) => (
            <li key={chapter.id}>
              <label
                className={chapter.has_text ? undefined : 'is-empty'}
                title={chapter.has_text ? undefined : 'not novelized yet: nothing to export'}
              >
                <input
                  type="checkbox"
                  disabled={!chapter.has_text}
                  checked={selected.has(chapter.id)}
                  onChange={() => toggle(chapter.id)}
                />
                <span className="pdf-chapter-number">
                  {String(n + 1).padStart(2, '0')}
                </span>
                <span>
                  {chapter.title}
                  {!chapter.has_text && <span className="muted small"> · not novelized yet</span>}
                </span>
              </label>
            </li>
          ))}
        </ol>
        {error && <p className="error">{error}</p>}
        <button
          className="stamp-button"
          disabled={busy || selected.size === 0}
          onClick={() => void runExport()}
        >
          {busy ? (format === 'pdf' ? 'rendering…' : 'writing…') : `export ${label}`}
        </button>
      </section>
    </div>
  )
}
