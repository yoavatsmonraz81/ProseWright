/** The shell: rail, page, right slot.
 *
 * Layout follows one rule — the page is the subject, everything else is
 * furniture. The rail holds one tab at a time and the right slot holds one pane
 * at a time, both collapsible, because a typewriter with three inspectors bolted
 * on stops being a typewriter.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError, streamPropose, type StreamEvent } from './api'
import { AnnotationBar } from './components/AnnotationBar'
import { ForkPane } from './components/ForkPane'
import { NovelPane } from './components/NovelPane'
import { PageView } from './components/PageView'
import { QueueTab } from './components/QueueTab'
import { Palette, type Jump } from './components/Palette'
import { AskTab } from './components/AskTab'
import { SpineTab } from './components/SpineTab'
import { TransformComposer } from './components/TransformComposer'
import { LogEditPanel } from './components/LogEditPanel'
import { TypographyPanel } from './components/TypographyPanel'
import { ModelControl } from './components/ModelControl'
import { VoicePicker } from './components/VoicePicker'
import { PdfExportSheet, type ExportFormat } from './components/PdfExportSheet'
import { HistoryDrawer } from './components/HistoryDrawer'
import { DriveSyncControl } from './components/DriveSyncControl'
import { CodeSyncControl } from './components/CodeSyncControl'
import { ProjectControl } from './components/ProjectControl'
import { CharPane } from './components/CharPane'
import { PlayPane } from './components/PlayPane'
import { resolveType } from './lib/typography'
import { verbsFor } from './lib/verbs'
import { isVoiced, voiceLabel } from './lib/voice'
import type {
  AskOptions,
  AskResult,
  DerivedBeat,
  EngineStatus,
  FontRegistry,
  Layer,
  LayerMap,
  ManuscriptView,
  PageMessage,
  Proposal,
  SceneRow,
  SpineView,
  StampReport,
  SweepReport,
  SyncStatus,
  WeedReport,
} from './types'
import './styles/tokens.css'
import './styles/fonts.css'
import './styles/app.css'

type RailTab = 'spine' | 'queue' | 'ask'
type Slot = 'fork' | 'file' | null

const SLOT_LABEL: Record<Exclude<Slot, null>, string> = {
  fork: 'fork',
  file: 'char',
}

const LAYERS: Layer[] = ['log', 'manuscript']

/** Names the book could follow, gathered from who speaks across the log. The
 *  book-level picker has no single scene to ask, so it asks all of them. */
function castHint(scenes: SceneRow[], focal?: string): { name: string; turns: number }[] {
  const counts = new Map<string, number>()
  for (const scene of scenes) {
    for (const speaker of scene.speakers) {
      counts.set(speaker, (counts.get(speaker) ?? 0) + 1)
    }
  }
  if (focal && !counts.has(focal)) counts.set(focal, 0)
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 12)
    .map(([name, turns]) => ({ name, turns }))
}

export default function App() {
  const [status, setStatus] = useState<EngineStatus | null>(null)
  const [scenes, setScenes] = useState<SceneRow[]>([])
  const [spine, setSpine] = useState<SpineView | null>(null)
  const [fonts, setFonts] = useState<FontRegistry | null>(null)
  const [novel, setNovel] = useState<ManuscriptView | null>(null)
  const [draftWordCounts, setDraftWordCounts] = useState<Record<string, number> | null>(null)
  const [stSync, setStSync] = useState<SyncStatus | null>(null)
  const [layerMap, setLayerMap] = useState<LayerMap | null>(null)
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [sweep, setSweep] = useState<SweepReport | null>(null)
  const [weeds, setWeeds] = useState<WeedReport | null>(null)
  const [stamps, setStamps] = useState<StampReport | null>(null)

  const [sceneId, setSceneId] = useState<number | null>(null)
  const [messages, setMessages] = useState<PageMessage[]>([])
  const [layer, setLayer] = useState<Layer>('log')
  // The Play view stands in front of the layers when the project has a play folder.
  const [playOpen, setPlayOpen] = useState(false)
  const [playConfigured, setPlayConfigured] = useState(false)
  useEffect(() => {
    api.play().then((v) => setPlayConfigured(v.configured)).catch(() => setPlayConfigured(false))
  }, [])
  const [railTab, setRailTab] = useState<RailTab>('spine')
  const [slot, setSlot] = useState<Slot>('fork')
  const [selection, setSelection] = useState<{ from: number; to: number } | null>(null)
  const [focusMessage, setFocusMessage] = useState<number | null>(null)

  const [verb, setVerb] = useState<string | null>(null)
  const [logEdit, setLogEdit] = useState(false)
  const [pageFilter, setPageFilter] = useState('all')
  const [voiceRows, setVoiceRows] = useState<{ voice: string; count: number; ids: number[] }[]>([])
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [typeOpen, setTypeOpen] = useState(false)
  const [voiceOpen, setVoiceOpen] = useState(false)
  const [exportFormat, setExportFormat] = useState<ExportFormat | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [spineBusy, setSpineBusy] = useState<'validate' | 'derive' | null>(null)
  const [spinePhase, setSpinePhase] = useState('')
  const [spineStep, setSpineStep] = useState(0)
  const [spineTotal, setSpineTotal] = useState(0)
  const [askOptions, setAskOptions] = useState<AskOptions | null>(null)
  const [askResult, setAskResult] = useState<AskResult | null>(null)
  const [asking, setAsking] = useState(false)
  const [askPhase, setAskPhase] = useState('')
  const [busy, setBusy] = useState(false)
  const [boot, setBoot] = useState<string | null>(null)
  const [gaps, setGaps] = useState<{ stale: string[]; broke: string[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [proseReviewRevision, setProseReviewRevision] = useState(0)
  const [compressionStatus, setCompressionStatus] = useState('')
  const [rhythmStatus, setRhythmStatus] = useState('')

  // Boot: everything the shell needs, in parallel and each on its own. A route
  // the engine has never heard of costs its own pane and says so, rather than
  // taking the screen down with it — a long-running engine started before the
  // page it is serving was built is the ordinary case, not a crash.
  useEffect(() => {
    let live = true
    void (async () => {
      const settled = await Promise.allSettled([
        api.status(),
        api.scenes(),
        api.spine(),
        api.fonts(),
        api.manuscript(),
        api.layers(),
        api.proposal(),
        api.voices(),
      ])
      if (!live) return
      const [st, sc, sp, fo, mv, lm, pr, ev] = settled

      const stale: string[] = []
      let missingLog = false
      const broke: string[] = []
      const took = <T,>(
        path: string,
        result: PromiseSettledResult<T>,
        keep: (value: T) => void,
      ) => {
        if (result.status === 'fulfilled') {
          keep(result.value)
          return
        }
        const reason = result.reason
        // "unknown path" means the engine answered and did not recognise the route,
        // which is version skew and asks for a restart. A missing log is not skew:
        // the routes exist; the project just has nothing in it yet.
        if (reason instanceof ApiError && reason.status === 404 && reason.message.startsWith('unknown path')) {
          stale.push(path)
        } else if (reason instanceof ApiError && reason.status === 404 && reason.message.startsWith('log not found')) {
          missingLog = true
        } else broke.push(`${path} — ${reason instanceof Error ? reason.message : String(reason)}`)
      }

      took('/status', st, setStatus)
      took('/scenes', sc, (v) => {
        setScenes(v.scenes)
        setSceneId((prev) => prev ?? (v.scenes.at(-1)?.scene_id ?? null))
      })
      took('/spine', sp, setSpine)
      took('/fonts', fo, setFonts)
      took('/manuscript', mv, setNovel)
      took('/layers', lm, setLayerMap)
      took('/log/by-voice', ev, (v) => setVoiceRows(v.voices ?? []))
      took('/proposal', pr, (v) => {
        setProposal(v)
        // A proposal left unanswered from a previous session is the first thing
        // the author needs to see, so the pane opens on it rather than waiting to
        // be found.
        if (v.kind) setSlot('fork')
      })

      // Nothing answered at all — the one failure that earns the whole screen.
      if (st.status === 'rejected' && !(st.reason instanceof ApiError)) {
        setBoot(st.reason instanceof Error ? st.reason.message : String(st.reason))
        return
      }
      if (missingLog) broke.push("This project's working log doesn't exist yet: play a turn, or put a log at the path its project.json names.")
      setGaps(stale.length || broke.length ? { stale, broke } : null)
    })()
    return () => {
      live = false
    }
  }, [])

  const scene = useMemo(
    () => scenes.find((s) => s.scene_id === sceneId) ?? null,
    [scenes, sceneId],
  )

  useEffect(() => {
    if (scene?.kind === 'frontmatter') setLayer('manuscript')
  }, [scene?.scene_id, scene?.kind])

  useEffect(() => {
    void api.syncStatus().then(setStSync).catch(() => setStSync(null))
  }, [status?.message_count])

  useEffect(() => {
    if (!scene) return
    if (scene.kind === 'frontmatter' || scene.start < 0) {
      setMessages([])
      return
    }
    let live = true
    void (async () => {
      try {
        const span = await api.span(scene.start, scene.end)
        if (live) setMessages(span.messages)
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      live = false
    }
  }, [scene])


  const sceneIndex = useMemo(
    () => scenes.findIndex((s) => s.scene_id === sceneId),
    [scenes, sceneId],
  )

  const goScene = useCallback(
    (delta: number) => {
      if (sceneIndex < 0) return
      const next = scenes[sceneIndex + delta]
      if (!next) return
      setSceneId(next.scene_id)
      setSelection(null)
      setFocusMessage(next.start)
    },
    [sceneIndex, scenes],
  )

  const jumpTo = useCallback(
    (msgId: number) => {
      const target = scenes.find((s) => s.start <= msgId && msgId <= s.end)
      if (target) {
        setSceneId(target.scene_id)
        if (target.kind === 'frontmatter') setLayer('manuscript')
      }
      setFocusMessage(msgId)
    },
    [scenes],
  )

  const pageMessages = useMemo(
    () => (pageFilter === 'all' ? messages : messages.filter((m) => isVoiced(m, pageFilter))),
    [messages, pageFilter],
  )

  const activeVoice = voiceRows.find((row) => row.voice === pageFilter)
  const voiceIds = activeVoice?.ids ?? []
  const voiceCursor = selection?.from ?? focusMessage
  const prevVoice =
    voiceCursor == null ? null : [...voiceIds].reverse().find((id) => id < voiceCursor) ?? null
  const nextVoice =
    voiceCursor == null ? voiceIds[0] ?? null : voiceIds.find((id) => id > voiceCursor) ?? null

  const goVoiceTurn = useCallback(
    (msgId: number) => {
      jumpTo(msgId)
      setSelection({ from: msgId, to: msgId })
    },
    [jumpTo],
  )

  // After a novelization or a verdict: the manuscript changed, and so did the
  // scene index that marks which scenes carry prose.
  const refreshNovel = useCallback(async () => {
    const [mv, sc] = await Promise.all([api.manuscript(), api.scenes()])
    setNovel(mv)
    setScenes(sc.scenes)
  }, [])

  const refreshProposal = useCallback(async () => {
    setProposal(await api.proposal())
  }, [])

  // A committed transform moves the log under everything above it: inserting a
  // turn shifts every id after it, and each pane reads ranges. So a verdict
  // re-reads the joins instead of trying to patch them in place.
  //
  // Injects often open a *new* scene (location header change). Staying on the
  // old scene_id makes the committed prose look like it vanished — jump to the
  // scene that holds the newest message and reload the page span.
  const refreshAfterWrite = useCallback(async (opts?: { focus?: number }) => {
    const [st, sc, pr, ev] = await Promise.all([
      api.status(),
      api.scenes(),
      api.proposal(),
      api.voices().catch(() => null),
    ])
    setStatus(st)
    setScenes(sc.scenes)
    setProposal(pr)
    if (ev) setVoiceRows(ev.voices ?? [])

    // Prefer the newest turn — injects append after the cursor, often into a
    // freshly segmented scene the previous sceneId does not cover. A remove
    // stays at the seam: the first surviving turn now lives at the old id.
    const last = Math.max(0, (st.message_count || 1) - 1)
    const focus =
      opts?.focus != null ? Math.min(Math.max(0, opts.focus), last) : last
    const target =
      sc.scenes.find((s) => s.start <= focus && focus <= s.end) ??
      sc.scenes[sc.scenes.length - 1] ??
      null
    if (target) {
      setSceneId(target.scene_id)
      setFocusMessage(focus)
      setMessages((await api.span(target.start, target.end)).messages)
    }
  }, [])

  const guard = useCallback(async (work: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await work()
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : String(e),
      )
    } finally {
      setBusy(false)
    }
  }, [])

  const compressCurrentScene = useCallback(() => {
    if (!scene) return
    const parts = scene.manuscript_parts?.length
      ? scene.manuscript_parts
      : scene.manuscript
        ? [{ ...scene.manuscript, start: scene.start, end: scene.end }]
        : []
    if (!parts.length) return
    void guard(async () => {
      setCompressionStatus('starting supervised manuscript compression…')
      let count = 0
      const warnings: string[] = []
      for (const [partIndex, part] of parts.entries()) {
        const result = await api.streamCompression(part.id, (event) => {
          if (event.kind === 'phase' && event.text) {
            setCompressionStatus(
              parts.length > 1
                ? `part ${partIndex + 1}/${parts.length} · ${event.text}`
                : event.text,
            )
          }
        })
        count += result.count
        warnings.push(...result.warnings)
      }
      setCompressionStatus(
        `${count} compression proposal${count === 1 ? '' : 's'} queued beneath the prose; nothing committed` +
        (warnings.length ? ` · ${warnings.length} guarded suggestion${warnings.length === 1 ? '' : 's'} skipped` : ''),
      )
      setProseReviewRevision((value) => value + 1)
    })
  }, [scene, guard])

  const smoothCurrentScene = useCallback(() => {
    if (!scene) return
    const parts = scene.manuscript_parts?.length
      ? scene.manuscript_parts
      : scene.manuscript
        ? [{ ...scene.manuscript, start: scene.start, end: scene.end }]
        : []
    if (!parts.length) return
    void guard(async () => {
      setRhythmStatus('starting phrasing and punctuation review…')
      let count = 0
      const warnings: string[] = []
      for (const [partIndex, part] of parts.entries()) {
        const result = await api.streamRhythm(part.id, (event) => {
          if (event.kind === 'phase' && event.text) {
            setRhythmStatus(
              parts.length > 1
                ? `part ${partIndex + 1}/${parts.length} · ${event.text}`
                : event.text,
            )
          }
        })
        count += result.count
        warnings.push(...result.warnings)
      }
      setRhythmStatus(
        `${count} phrasing proposal${count === 1 ? '' : 's'} queued beneath the prose; nothing committed` +
        (warnings.length ? ` · ${warnings.length} guarded suggestion${warnings.length === 1 ? '' : 's'} skipped` : ''),
      )
      setProseReviewRevision((value) => value + 1)
    })
  }, [scene, guard])

  useEffect(() => {
    setCompressionStatus('')
    setRhythmStatus('')
  }, [sceneId])

  const setVoice = useCallback(
    (voice: string, mode: string) => {
      if (!selection) return
      void guard(async () => {
        const ids: number[] = []
        for (let id = selection.from; id <= selection.to; id += 1) ids.push(id)
        await api.setVoice(ids, voice, mode)
        if (scene) setMessages((await api.span(scene.start, scene.end)).messages)
        const catalog = await api.voices().catch(() => null)
        if (catalog) setVoiceRows(catalog.voices ?? [])
      })
    },
    [selection, scene, guard],
  )

  const acceptProposal = useCallback(
    (allowFlagged: boolean) => {
      const kind = proposal?.kind
      if (!kind) return
      const wantSweep = Boolean(proposal?.meta?.sweep)
      const isRemove = proposal?.operator === 'remove'
      const seam = proposal?.edits?.[0]?.msg_id
      void guard(async () => {
        await api.acceptEdits(allowFlagged)
        setSelection(null)
        if (isRemove && seam != null) await refreshAfterWrite({ focus: seam })
        else await refreshAfterWrite()
        if (wantSweep) {
          const result = await api.sweepLast()
          setSweep(result.report)
          setSlot('fork')
        }
      })
    },
    [proposal, guard, refreshAfterWrite],
  )

  const removeHere = useCallback(
    (sweep: boolean) => {
      if (!selection) return
      void guard(async () => {
        await api.proposeRemove(selection.from, selection.to, sweep)
        setSelection(null)
        await refreshProposal()
        setSlot('fork')
      })
    },
    [selection, guard, refreshProposal],
  )

  const rejectProposal = useCallback(() => {
    const kind = proposal?.kind
    if (!kind) return
    void guard(async () => {
      await api.rejectEdits()
      await refreshProposal()
    })
  }, [proposal, guard, refreshProposal])

  const dropEdit = useCallback(
    (msgId: number) => {
      void guard(async () => {
        await api.dropEdit(msgId)
        await refreshProposal()
      })
    },
    [guard, refreshProposal],
  )

  const acceptOneEdit = useCallback(
    (index: number, allowFlagged: boolean) => {
      const focus = proposal?.edits?.[index]?.msg_id
      void guard(async () => {
        await api.acceptOneEdit(index, allowFlagged)
        await refreshAfterWrite(focus != null ? { focus } : undefined)
        setSlot('fork')
      })
    },
    [proposal, guard, refreshAfterWrite],
  )

  const dropEditAt = useCallback(
    (index: number) => {
      void guard(async () => {
        await api.dropEditAt(index)
        await refreshProposal()
      })
    },
    [guard, refreshProposal],
  )

  const runSpineJob = useCallback(
    (
      kind: 'validate' | 'derive',
      run: (onEvent: (event: StreamEvent) => void) => Promise<SpineView>,
    ) => {
      setSpineBusy(kind)
      setSpinePhase('starting…')
      setSpineStep(0)
      setSpineTotal(0)
      setError(null)
      void (async () => {
        try {
          setSpine(
            await run((event) => {
              if (event.kind === 'phase') {
                setSpinePhase(event.text ?? '')
                if (typeof event.step === 'number') setSpineStep(event.step)
                if (typeof event.total === 'number') setSpineTotal(event.total)
              }
            }),
          )
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e))
        } finally {
          setSpineBusy(null)
          setSpinePhase('')
          setSpineStep(0)
          setSpineTotal(0)
        }
      })()
    },
    [],
  )

  const validate = useCallback(() => {
    runSpineJob('validate', (onEvent) => api.streamValidateSpine(onEvent))
  }, [runSpineJob])

  const deriveSpine = useCallback(() => {
    runSpineJob('derive', (onEvent) => api.streamDeriveSpine(onEvent))
  }, [runSpineJob])

  const commitSpine = useCallback(() => {
    void guard(async () => {
      setSpine(await api.commitSpine())
    })
  }, [guard])

  const discardSpine = useCallback(() => {
    void guard(async () => {
      setSpine(await api.discardSpine())
    })
  }, [guard])

  const refreshAskOptions = useCallback(async () => {
    try {
      setAskOptions(await api.askOptions())
    } catch {
      // Ask options are optional furniture; the tab still works with typed as_of.
    }
  }, [])

  useEffect(() => {
    if (railTab === 'ask' && !askOptions) {
      void refreshAskOptions()
    }
  }, [railTab, askOptions, refreshAskOptions])

  const runAsk = useCallback(
    (question: string, asOf: string, mode: 'fused' | 'keyword') => {
      setAsking(true)
      setAskPhase('starting…')
      setError(null)
      void (async () => {
        try {
          setAskResult(
            await api.streamAsk(
              question,
              { as_of: asOf || undefined, mode },
              (event) => {
                if (event.kind === 'phase') {
                  setAskPhase(event.text ?? '')
                }
              },
            ),
          )
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e))
        } finally {
          setAsking(false)
          setAskPhase('')
        }
      })()
    },
    [],
  )

  const pickBeat = useCallback(
    (beat: DerivedBeat) => jumpTo(beat.start_msg_id),
    [jumpTo],
  )

  const verbs = useMemo(
    () =>
      verbsFor(layerMap, layer, {
        selection: selection !== null,
        sweepReady: Boolean(status?.sweep_ready),
      }),
    [layerMap, layer, selection, status],
  )

  // Restyle narrows by speaker and inject needs a name to write as; both are
  // answered by who actually speaks inside the selection.
  const selectionCast = useMemo(() => {
    if (!selection) return []
    const names = messages
      .filter((m) => m.msg_id >= selection.from && m.msg_id <= selection.to)
      .map((m) => m.speaker)
    return [...new Set(names)]
  }, [messages, selection])

  const openComposer = useCallback((name: string) => {
    setSweep(null)
    setWeeds(null)
    setStamps(null)
    setVerb(name)
  }, [])

  // "propose…" and the p key do not name a verb, so the sheet opens on the first
  // one the engine will actually run here rather than on a fixed guess that the
  // layer might not allow.
  const proposeHere = useCallback(() => {
    const first = verbs.find((v) => v.state === 'ready') ?? verbs[0]
    if (first) openComposer(first.name)
  }, [verbs, openComposer])

  // Keyboard map. Ignored while typing, so a note that starts with "j" is a note.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      const typing =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable)
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
        return
      }
      if (typing) return
      if (e.key === 'Escape') {
        if (verb) {
          setVerb(null)
          return
        }
        setSelection(null)
        setPaletteOpen(false)
        setTypeOpen(false)
        return
      }
      switch (e.key) {
        case 'j':
          goScene(1)
          break
        case 'k':
          goScene(-1)
          break
        case 'l':
          setLayer(LAYERS[(LAYERS.indexOf(layer) + 1) % LAYERS.length])
          break
        case '\\':
          setSlot((s) => (s ? null : 'fork'))
          break
        case 's':
          setRailTab('spine')
          break
        case 'q':
          setRailTab('queue')
          break
        case 'a':
          setRailTab('ask')
          break
        case 't':
          setTypeOpen((v) => !v)
          break
        case 'p':
          proposeHere()
          break
        default:
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [goScene, layer, verb, proposeHere])

  const type = resolveType(fonts, layer)

  if (boot) {
    return (
      <div className="boot">
        <h1>ProseWright</h1>
        <p className="error">{boot}</p>
        <p className="muted">
          Nothing is answering at {window.location.origin}. Start the engine with{' '}
          <code>python -m story_editor serve</code> and reload.
        </p>
      </div>
    )
  }

  return (
    <div className={`shell ${slot ? '' : 'slot-closed'}`}>
      <aside className="rail">
        <nav className="rail-tabs">
          {(['spine', 'queue', 'ask'] as RailTab[]).map((tab) => (
            <button
              key={tab}
              className={railTab === tab ? 'is-on' : ''}
              onClick={() => setRailTab(tab)}
            >
              {tab}
            </button>
          ))}
        </nav>
        {railTab === 'spine' && (
          <SpineTab
            spine={spine}
            novel={novel}
            showWordCounts={layer === 'manuscript'}
            draftWordCounts={draftWordCounts}
            activeBeat={scene?.beat_id ?? null}
            onPickBeat={pickBeat}
            onValidate={validate}
            onDerive={deriveSpine}
            onCommit={commitSpine}
            onDiscard={discardSpine}
            busy={spineBusy}
            busyPhase={spinePhase}
            busyStep={spineStep}
            busyTotal={spineTotal}
            error={error}
          />
        )}
        {railTab === 'queue' && (
          <QueueTab
            novel={novel}
            scenes={scenes}
            activeScene={sceneId}
            onPick={(msgId) => {
              jumpTo(msgId)
              setLayer('manuscript')
            }}
            onSetBookVoice={() => setVoiceOpen(true)}
            onRefresh={() => void refreshNovel()}
            reviewRevision={proseReviewRevision}
          />
        )}
        {railTab === 'ask' && (
          <AskTab
            options={askOptions}
            result={askResult}
            busy={asking}
            phase={askPhase}
            error={error}
            onAsk={runAsk}
            onCite={jumpTo}
          />
        )}
      </aside>

      <main className="center">
        <header className="page-head">
          <div className="page-title">
            <h2>
              {playOpen
                ? 'play'
                : scene
                ? scene.kind === 'frontmatter'
                  ? scene.location
                  : `${scene.kind === 'interlude' ? 'interlude' : 'scene'} ${scene.scene_id}`
                : 'no scene'}
            </h2>
            <span className="page-meta">
              {playOpen
                ? 'Player mode · every turn goes into the log'
                : scene?.kind === 'frontmatter'
                ? 'front matter · not in the log'
                : (
                  <>
                    {scene?.date} {scene?.time_start} · {scene?.location}
                    {scene?.episode_id !== null && scene?.episode_id !== undefined ? (
                      <> · episode {scene.episode_id + 1} · {scene.episode_title}</>
                    ) : scene?.beat_id !== null && scene?.beat_id !== undefined && (
                      <> · beat {scene.beat_id}</>
                    )}
                  </>
                )}
            </span>
          </div>
          <div className="layer-toggle" role="tablist">
            {playConfigured && (
              <button
                role="tab"
                aria-selected={playOpen}
                className={playOpen ? 'is-on' : ''}
                onClick={() => setPlayOpen(true)}
                title="play the story with the Player-mode engine"
              >
                play
              </button>
            )}
            {LAYERS.map((l) => (
              <button
                key={l}
                role="tab"
                aria-selected={!playOpen && layer === l}
                className={!playOpen && layer === l ? 'is-on' : ''}
                onClick={() => {
                  setPlayOpen(false)
                  setLayer(l)
                }}
              >
                {l === 'manuscript' ? 'novel' : l}
              </button>
            ))}
          </div>
          {!playOpen && layer === 'log' && scene?.kind !== 'frontmatter' && (
            <div className="page-filter" title="show only one character's own turns">
              <button
                className={`chip ${pageFilter === 'all' ? 'is-on' : ''}`}
                onClick={() => setPageFilter('all')}
              >
                everyone
              </button>
              <select
                className={`chip page-filter-select ${pageFilter !== 'all' ? 'is-on' : ''}`}
                value={pageFilter === 'all' ? '' : pageFilter}
                onChange={(e) => setPageFilter(e.target.value || 'all')}
                aria-label="filter by character"
              >
                <option value="">character…</option>
                {voiceRows.map((row) => (
                  <option key={row.voice} value={row.voice}>
                    {voiceLabel(row.voice)} · {row.count}
                  </option>
                ))}
              </select>
              <button
                className="chip ghost"
                disabled={prevVoice == null}
                onClick={() => prevVoice != null && goVoiceTurn(prevVoice)}
                title={pageFilter === 'all' ? 'pick a character first' : `previous ${voiceLabel(pageFilter)}`}
              >
                prev
              </button>
              <button
                className="chip ghost"
                disabled={nextVoice == null}
                onClick={() => nextVoice != null && goVoiceTurn(nextVoice)}
                title={pageFilter === 'all' ? 'pick a character first' : `next ${voiceLabel(pageFilter)}`}
              >
                next
              </button>
              <button
                className={`chip ${logEdit ? 'is-on' : ''}`}
                disabled={messages.length === 0}
                onClick={() => {
                  if (logEdit) {
                    if (
                      !window.confirm(
                        'Discard the working copy? Nothing has been written to the log.',
                      )
                    ) {
                      return
                    }
                  }
                  setLogEdit((on) => !on)
                }}
                title="copy this scene out, edit, then approve a diff back in"
              >
                edit
              </button>
            </div>
          )}
          <div className="page-tools">
            {layer === 'manuscript' && (
              <button
                className="chip ghost"
                onClick={() => setExportFormat('pdf')}
                title="export selected chapters to PDF"
              >
                PDF
              </button>
            )}
            {layer === 'manuscript' && (
              <button
                className="chip ghost"
                onClick={() => setExportFormat('txt')}
                title="export selected chapters as plain text, one file per chapter"
              >
                TXT
              </button>
            )}
            {layer === 'manuscript' && (
              <button
                className="chip ghost"
                onClick={() => setExportFormat('ao3')}
                title="export selected chapters as AO3-ready HTML, one file per chapter"
              >
                AO3
              </button>
            )}
            <button className="chip ghost" onClick={() => setTypeOpen(true)} title="typography (t)">
              Aa
            </button>
            <button
              className="chip ghost"
              onClick={() => setSlot((s) => (s ? null : 'fork'))}
              title="right pane (\)"
            >
              {slot ? '›' : '‹'}
            </button>
          </div>
        </header>

        {gaps && (
          <div className="gap-note">
            <div>
              {gaps.stale.length > 0 && (
                <p>
                  The engine answered, but does not know {gaps.stale.join(', ')}. It is
                  running older code than this page — restart it with{' '}
                  <code>python -m story_editor serve</code>, then reload.
                </p>
              )}
              {gaps.broke.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </div>
            <button className="chip ghost" onClick={() => setGaps(null)}>
              dismiss
            </button>
          </div>
        )}

        {!playOpen && scene?.synopsis && <p className="synopsis">{scene.synopsis}</p>}

        {playOpen ? (
          <PlayPane onLogChanged={() => void refreshAfterWrite()} />
        ) : layer === 'log' && logEdit ? (
          <LogEditPanel
            messages={messages}
            focusMessage={focusMessage}
            busy={busy}
            error={error}
            onCancel={() => setLogEdit(false)}
            onPropose={(patches) => {
              void guard(async () => {
                await api.proposePatch(patches)
                setLogEdit(false)
                setSlot('fork')
                await refreshProposal()
              })
            }}
          />
        ) : layer === 'log' && scene?.kind === 'frontmatter' ? (
          <div className="page empty-layer">
            <p>This is front matter. It is not in the log.</p>
            <p className="muted">
              Open the novel layer to read it on the same paper as the rest of
              the book.
            </p>
          </div>
        ) : layer === 'log' ? (
          <PageView
            messages={pageMessages}
            selection={selection}
            focusMessage={focusMessage}
            onSelect={setSelection}
            fontFamily={`"${type.family}", var(--font-mono)`}
            fontSize={type.size}
            lineHeight={type.lineHeight}
            measure={type.measure}
            tracking={type.tracking}
          />
        ) : layer === 'manuscript' && scene ? (
          <NovelPane
            scene={scene}
            type={type}
            onNovelized={() => void refreshNovel()}
            onReviewChanged={() => setProseReviewRevision((value) => value + 1)}
            onDraftWordCounts={setDraftWordCounts}
            reviewRevision={proseReviewRevision}
          />
        ) : (
          <div className="page empty-layer">
            <p>Pick a scene to read it as prose.</p>
          </div>
        )}

        {selection && !playOpen && layer === 'log' && !logEdit && (
          <AnnotationBar
            range={selection}
            busy={busy}
            error={error}
            onVoice={setVoice}
            speakers={selectionCast}
            onPropose={proposeHere}
            onRemove={removeHere}
            cast={voiceRows.map((r) => r.voice)}
            onDismiss={() => setSelection(null)}
          />
        )}

        <footer className="status">
          <span>{status ? `${status.message_count} messages` : '—'}</span>
          {stSync && !stSync.in_sync && (
            <button
              className="link"
              disabled={busy}
              title={`${stSync.kind}: working ${stSync.working_count} · ST file ${stSync.st_count}${stSync.changed_message_count ? ` · ${stSync.changed_message_count} changed (${stSync.changed_msg_ids?.join(', ')})` : ''}. Writes the chat file; reload the chat in SillyTavern without saving first.`}
              onClick={() => {
                void guard(async () => {
                  await api.syncPush()
                  setStSync(await api.syncStatus())
                })
              }}
            >
              ST {stSync.kind.replace(/_/g, ' ')} · write chat file
            </button>
          )}
          <ProjectControl />
          <ModelControl status={status} onChanged={setStatus} />
          <DriveSyncControl />
          <CodeSyncControl />
          <button
            className="link"
            onClick={() => setHistoryOpen(true)}
            title="browse accepted log changes and undo the latest one"
          >
            history{status?.history_entries ? ` · ${status.history_entries}` : ''}
          </button>
          <span>
            {scenes.length} scenes
          </span>
          {proposal?.kind && (
            <button className="link" onClick={() => setSlot('fork')}>
              a proposal is waiting for a verdict
            </button>
          )}
          <span className="keys">
            j/k scene · l layer · p propose · \ pane · ⌘K jump
          </span>
        </footer>
      </main>

      {slot && (
        <aside className="slot">
          <nav className="slot-tabs">
            {(['fork', 'file'] as Exclude<Slot, null>[]).map((s) => (
              <button key={s} className={slot === s ? 'is-on' : ''} onClick={() => setSlot(s)}>
                {SLOT_LABEL[s]}
              </button>
            ))}
          </nav>
          {slot === 'fork' && (
            <ForkPane
              layer={layer}
              verbs={verbs}
              proposal={proposal}
              sweep={sweep}
              weeds={weeds}
              stamps={stamps}
              novel={novel}
              scene={scene}
              compressionStatus={compressionStatus}
              busy={busy}
              error={error}
              onVerb={openComposer}
              onAccept={acceptProposal}
              onAcceptOne={acceptOneEdit}
              onReject={rejectProposal}
              onDrop={dropEdit}
              onDropAt={dropEditAt}
              onJump={jumpTo}
              onClearSweep={() => setSweep(null)}
              onClearWeeds={() => setWeeds(null)}
              onProposeWeeds={() => {
                if (!weeds) return
                void guard(async () => {
                  await streamPropose(
                    '/weed',
                    { from: weeds.from, to: weeds.to },
                    () => undefined,
                  )
                  setWeeds(null)
                  await refreshProposal()
                })
              }}
              onClearStamps={() => setStamps(null)}
              onProposeStamps={() => {
                if (!stamps) return
                void guard(async () => {
                  await streamPropose(
                    '/stamps',
                    { from: stamps.from, to: stamps.to },
                    () => undefined,
                  )
                  setStamps(null)
                  await refreshProposal()
                })
              }}
              onRebaseScene={(id) => {
                void guard(async () => {
                  await api.rebaseScene(id)
                  await refreshNovel()
                })
              }}
              onPinScene={(id) => {
                void guard(async () => {
                  await api.pinScene(id)
                  await refreshNovel()
                })
              }}
              onCompressScene={compressCurrentScene}
              onSmoothScene={smoothCurrentScene}
              rhythmStatus={rhythmStatus}
            />
          )}
          {slot === 'file' && <CharPane onJump={jumpTo} />}
        </aside>
      )}

      {verb && (
        <TransformComposer
          verbs={verbs}
          verb={verb}
          range={selection}
          sceneRange={scene ? { from: scene.start, to: scene.end } : null}
          speakers={selectionCast}
          onVerb={setVerb}
          onClose={() => setVerb(null)}
          onProposed={() => {
            setVerb(null)
            setWeeds(null)
            setStamps(null)
            setSlot('fork')
            void guard(refreshProposal)
          }}
          onSwept={(report) => {
            setVerb(null)
            setSweep(report)
            setSlot('fork')
          }}
          onWeeded={(report) => {
            setVerb(null)
            setWeeds(report)
            setSlot('fork')
          }}
          onStamped={(report) => {
            setVerb(null)
            setStamps(report)
            setSlot('fork')
          }}
        />
      )}

      {paletteOpen && (
        <Palette
          scenes={scenes}
          beats={spine?.derived ?? []}
          unitType={spine?.unit_type ?? 'beat'}
          onJump={(jump: Jump) => {
            jumpTo(jump.msgId)
            setPaletteOpen(false)
          }}
          onClose={() => setPaletteOpen(false)}
        />
      )}

      {voiceOpen && novel && (
        <div className="sheet-scrim" onClick={() => setVoiceOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <header>
              <h3>The book’s voice</h3>
              <button className="chip ghost" onClick={() => setVoiceOpen(false)}>
                done
              </button>
            </header>
            <p className="muted small">
              Every scene novelized from here on is written this way. Scenes already
              written keep the voice they were written in — changing this is a
              decision about what comes next, not a rewrite of what exists.
            </p>
            <VoicePicker
              voice={novel.voice}
              persons={novel.persons}
              tenses={novel.tenses}
              cast={castHint(scenes, novel.voice.focal)}
              onChange={(patch) => {
                void guard(async () => {
                  await api.setBookVoice(patch)
                  await refreshNovel()
                })
              }}
            />
            <p className="muted small">
              Currently {novel.voice_label}.
            </p>
          </div>
        </div>
      )}

      {exportFormat && (
        <PdfExportSheet
          format={exportFormat}
          onClose={() => setExportFormat(null)}
        />
      )}

      {historyOpen && (
        <HistoryDrawer
          scenes={scenes}
          activeScene={sceneId}
          onClose={() => setHistoryOpen(false)}
          onJump={(msgId) => {
            jumpTo(msgId)
            setHistoryOpen(false)
          }}
          onUndone={async (focus) => {
            await refreshAfterWrite(focus == null ? undefined : { focus })
            await refreshNovel()
            setStSync(await api.syncStatus().catch(() => null))
          }}
        />
      )}

      {typeOpen && (
        <TypographyPanel
          fonts={fonts}
          // Opens on the role the visible layer actually reads, so a change is
          // seen rather than filed against a role that is not on screen.
          openRole={type.role}
          effective={type}
          sample={messages[0]?.prose.slice(0, 600) ?? ''}
          onChange={setFonts}
          onClose={() => setTypeOpen(false)}
        />
      )}
    </div>
  )
}
