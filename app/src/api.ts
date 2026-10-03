/** The engine, as functions.
 *
 * One place that knows about HTTP. Errors carry the server's own message —
 * the API answers 409 with a sentence explaining which layer rule was broken,
 * and that sentence is more useful to the user than "request failed".
 */

import type {
  AskOptions,
  AskResult,
  DerivedScene,
  EngineStatus,
  FontRegistry,
  Layer,
  LayerMap,
  LogSpan,
  LogSceneView,
  ManuscriptView,
  ModelSettings,
  NovelizeBatchResult,
  NovelizePlan,
  NovelizeResult,
  Proposal,
  ProseReviewProposal,
  ProseReviewSummary,
  SceneRow,
  SceneStatus,
  SpineView,
  StampReport,
  SweepReport,
  SyncPushResult,
  SyncStatus,
  Voice,
  WeedReport,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const raw = await res.text()
  let body: unknown = null
  if (raw) {
    try {
      body = JSON.parse(raw)
    } catch {
      body = { error: raw }
    }
  }
  if (!res.ok) {
    const message =
      (body as { error?: string } | null)?.error ?? `${res.status} ${res.statusText}`
    throw new ApiError(res.status, message)
  }
  return body as T
}

const get = <T,>(path: string) => request<T>(path)
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

async function downloadExport(
  route: string,
  chapterIds: number[],
  fallbackName: string,
  extra: Record<string, unknown> = {},
): Promise<void> {
  const res = await fetch(route, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chapter_ids: chapterIds, ...extra }),
  })
  if (!res.ok) {
    const raw = await res.text()
    let message = `${res.status} ${res.statusText}`
    try {
      message = (JSON.parse(raw) as { error?: string }).error ?? message
    } catch {
      if (raw) message = raw
    }
    throw new ApiError(res.status, message)
  }
  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? fallbackName
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/** A .sebundle of the open project, downloaded through the browser. */
const saveProjectBundle = (opts: { with_backups?: boolean; with_index?: boolean } = {}) =>
  downloadExport('/project/save', [], 'project.sebundle', opts)

const exportPdf = (chapterIds: number[]) =>
  downloadExport('/export/pdf', chapterIds, 'story.pdf')

/** One .txt per chapter; the engine zips them when more than one is selected. */
const exportTxt = (chapterIds: number[]) =>
  downloadExport('/export/txt', chapterIds, 'story-txt.zip')

/** A chapter the exporters can write: the locked chapter map's, or one per scene. */
export interface ExportChapter {
  id: number
  title: string
  start: number
  end: number
  /** Whether the novel layer has prose for it yet. */
  has_text: boolean
}

/** One AO3-ready HTML body per chapter (paste into AO3's HTML editor). */
const exportAo3 = (chapterIds: number[]) =>
  downloadExport('/export/ao3', chapterIds, 'story-ao3.zip')

/** One event off a proposing route's SSE stream. `phase` lines say which message
 *  the operator has reached; `reasoning` and `content` are the model thinking and
 *  writing. */
export interface StreamEvent {
  kind: string
  text?: string
  error?: string
  stage?: string
  step?: number
  total?: number
  spine?: SpineView
  ask?: AskResult
  novelize?: NovelizeResult
  batch?: NovelizeBatchResult
  ok?: boolean
  saved?: boolean
  scene?: DerivedScene
  warnings?: string[]
  rhythm?: { proposals: ProseReviewProposal[]; count: number; warnings: string[] }
  compression?: { proposals: ProseReviewProposal[]; count: number; warnings: string[] }
}

/** Run a proposing route with `stream: true` and forward its events.
 *
 * These operators take minutes on a local model — a span restyle is one call per
 * message — so the alternative is a spinner that cannot say whether anything is
 * happening. Resolves when the engine sends `done`; throws its own sentence on
 * `error`, which is the same text the blocking route would have returned.
 */
export async function streamPropose(
  path: string,
  body: Record<string, unknown>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, stream: true }),
  })
  if (!res.ok || !res.body) {
    const raw = await res.text()
    let message = `${res.status} ${res.statusText}`
    try {
      message = (JSON.parse(raw) as { error?: string }).error ?? message
    } catch {
      if (raw) message = raw
    }
    throw new ApiError(res.status, message)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const verdicts: StreamEvent[] = []

  const drain = () => {
    // SSE frames end on a blank line; a chunk boundary can land anywhere, so
    // whatever follows the last complete frame stays in the buffer.
    for (;;) {
      const end = buffer.indexOf('\n\n')
      if (end === -1) return
      const frame = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue
        let event: StreamEvent
        try {
          event = JSON.parse(line.slice(5).trim()) as StreamEvent
        } catch {
          continue
        }
        onEvent(event)
        if (event.kind === 'done' || event.kind === 'error') verdicts.push(event)
      }
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (value) {
      buffer += decoder.decode(value, { stream: true })
      drain()
    }
    // The verdict ends the work, so it ends the read. Waiting for the socket to
    // close as well leaves the operator apparently still running long after the
    // prose has arrived, for as long as the engine holds the connection.
    if (verdicts.length) {
      void reader.cancel()
      break
    }
    if (done) {
      buffer += decoder.decode()
      drain()
      break
    }
  }

  const verdict = verdicts.at(-1)
  if (!verdict) {
    throw new ApiError(502, 'the engine closed the stream without a verdict')
  }
  if (verdict.kind === 'error') {
    throw new ApiError(502, verdict.error ?? 'the operator failed')
  }
}

export const api = {
  exportPdf,
  exportTxt,
  exportAo3,
  exportChapters: () => get<{ chapters: ExportChapter[] }>('/export/chapters'),
  status: () => get<EngineStatus>('/status'),
  model: () => get<ModelSettings & { reachable: boolean }>('/model'),
  setModel: (body: {
    provider?: string
    model?: string
    api_key?: string
    base_url?: string
    local_port?: number
  }) => post<ModelSettings & { ok: boolean; reachable: boolean }>('/model', body),
  layers: () => get<LayerMap>('/layers'),
  syncStatus: () => get<SyncStatus>('/sync/status'),
  syncPush: () => post<SyncPushResult>('/sync/push'),
  play: () => get<import('./types').PlayView>('/play'),
  playTurn: (move: string, debug = false) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/turn', { move, debug }),
  playBegin: (debug = false) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/begin', { debug }),
  playUndo: () => post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/undo'),
  playAs: (char: string) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/as', { char }),
  playSync: () => post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/sync'),
  playRenarrate: (debug = false) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/renarrate', { debug }),
  playReroll: (debug = false) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/reroll', { debug }),
  playSwipe: (index: number) =>
    post<{ view: import('./types').PlayView } & Record<string, unknown>>('/play/swipe', { index }),
  cast: () => get<import('./types').CastView>('/cast'),
  castMember: (key: string) => get<import('./types').CastMember>(`/cast/${encodeURIComponent(key)}`),
  projects: () => get<import('./types').ProjectsView>('/projects'),
  addProject: (home: string) => post<import('./types').ProjectsView>('/project/add', { home }),
  switchProject: (id: string) =>
    post<{ ok: boolean; restarting: boolean }>('/project/switch', { id }),
  saveProjectBundle,
  /** Upload a .sebundle; the engine checks it and says what loading would do. */
  uploadBundle: (file: File) =>
    request<import('./types').StagedUpload>('/project/upload', {
      method: 'POST',
      body: file,
      headers: { 'Content-Type': 'application/octet-stream' },
    }),
  loadBundle: (token: string, id: string) =>
    post<import('./types').ProjectsView & { loaded: import('./types').LoadedProject }>(
      '/project/load', { token, id }),
  discardUpload: (token: string) => post<{ ok: boolean }>('/project/upload/discard', { token }),
  codeStatus: () => get<import('./types').CodeStatus>('/code/status'),
  codeUpdate: () => post<import('./types').CodeUpdateResult>('/code/update', { restart: true }),
  codePush: () => post<{ ok: boolean; status: import('./types').CodeStatus }>('/code/push'),
  codeRestart: () => post<{ ok: boolean; restarting: boolean }>('/code/restart'),
  driveStatus: () => get<import('./types').DriveStatus>('/drive/status'),
  driveStart: (direction: 'push' | 'pull', force = false) =>
    post<{ ok: boolean; job: import('./types').DriveJob }>(`/drive/${direction}`, { force }),
  driveJob: () => get<import('./types').DriveJob>('/drive/job'),
  history: (limit = 100) =>
    get<{ entries: import('./types').HistoryEntry[]; path: string }>(
      `/history?limit=${encodeURIComponent(limit)}`,
    ),
  undoEdits: (historyId?: number) =>
    post<{ ok: boolean; restored: string }>(
      '/edits/undo',
      historyId == null ? {} : { history_id: historyId },
    ),

  spine: () => get<SpineView>('/spine'),
  /** Blocking validate — prefer ``streamValidateSpine`` in the UI. */
  validateSpine: () => post<SpineView>('/spine/validate'),
  /**
   * Streaming validate with phase/step progress. Resolves to the fresh spine
   * view once the audit finishes (and has been cached server-side).
   */
  streamValidateSpine: async (
    onEvent: (event: StreamEvent) => void,
  ): Promise<SpineView> => {
    let spine: SpineView | null = null
    await streamPropose('/spine/validate', {}, (event) => {
      onEvent(event)
      if (event.kind === 'done' && event.spine) {
        spine = event.spine
      }
    })
    if (!spine) {
      throw new ApiError(502, 'spine validate finished without a spine payload')
    }
    return spine
  },
  /**
   * Streaming derive — writes a *pending* proposal. Commit separately so the
   * working derived spine only changes when the director signs off.
   */
  streamDeriveSpine: async (
    onEvent: (event: StreamEvent) => void,
  ): Promise<SpineView> => {
    let spine: SpineView | null = null
    await streamPropose('/spine/derive', {}, (event) => {
      onEvent(event)
      if (event.kind === 'done' && event.spine) {
        spine = event.spine
      }
    })
    if (!spine) {
      throw new ApiError(502, 'spine derive finished without a spine payload')
    }
    return spine
  },
  commitSpine: () => post<SpineView>('/spine/commit'),
  discardSpine: () => post<SpineView>('/spine/discard'),

  askOptions: () => get<AskOptions>('/ask/options'),
  streamAsk: async (
    question: string,
    opts: { as_of?: string; mode?: 'fused' | 'keyword' } = {},
    onEvent: (event: StreamEvent) => void,
  ): Promise<AskResult> => {
    let ask: AskResult | null = null
    await streamPropose(
      '/ask',
      {
        question,
        as_of: opts.as_of || undefined,
        mode: opts.mode || 'fused',
      },
      (event) => {
        onEvent(event)
        if (event.kind === 'done' && event.ask) {
          ask = event.ask
        }
      },
    )
    if (!ask) {
      throw new ApiError(502, 'ask finished without an answer payload')
    }
    return ask
  },

  scenes: () => get<{ scenes: SceneRow[]; count: number }>('/scenes'),
  span: (from: number, to: number) => get<LogSpan>(`/log/span?from=${from}&to=${to}`),
  voices: () =>
    get<{ ok: boolean; voices: { voice: string; count: number; ids: number[] }[] }>(
      '/log/by-voice',
    ),

  manuscript: (layer: Layer = 'manuscript') =>
    get<ManuscriptView>(`/manuscript?layer=${layer}`),
  scene: (id: string, layer: Layer = 'manuscript') =>
    get<DerivedScene>(`/scene/${id}?layer=${layer}`),
  setSceneStatus: (id: string, status: SceneStatus, layer: Layer = 'manuscript') =>
    post<DerivedScene>(`/scene/${id}/status`, { status, layer }),
  rebaseScene: (id: string, layer: Layer = 'manuscript') =>
    post<DerivedScene>(`/scene/${id}/rebase`, { layer }),
  pinScene: (id: string, layer: Layer = 'manuscript', unpin = false) =>
    post<DerivedScene>(`/scene/${id}/pin`, { layer, unpin }),
  sceneLog: (id: string) => get<LogSceneView>(`/scene/${id}?layer=log`),
  editScene: (id: string, text: string, layer: Layer = 'manuscript') =>
    request<DerivedScene>(`/scene/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ layer, text }),
    }),
  proseReview: (sceneId: string, status = 'pending') => {
    const q = new URLSearchParams({ scene_id: sceneId, status })
    return get<{ proposals: ProseReviewProposal[]; count: number }>(`/prose-review?${q}`)
  },
  proseReviewSummary: () => get<ProseReviewSummary>('/prose-review/summary'),
  commitProseProposal: (id: string, replacements?: Record<string, string>) =>
    post<{
      ok: boolean
      proposal: ProseReviewProposal
      scene: DerivedScene
      backup: string
      autosave: string
    }>(`/prose-review/${id}/commit`, { replacements }),
  proposeRhythm: (sceneId: string) =>
    post<{ proposals: ProseReviewProposal[]; count: number; warnings: string[] }>(
      '/prose-review/rhythm', { scene_id: sceneId },
    ),
  streamRhythm: async (sceneId: string, onEvent: (event: StreamEvent) => void) => {
    let result: StreamEvent['rhythm'] | undefined
    await streamPropose('/prose-review/rhythm', { scene_id: sceneId }, (event) => {
      onEvent(event)
      if (event.kind === 'done') result = event.rhythm
    })
    if (!result) throw new ApiError(502, 'Rhythm repair finished without a result payload')
    return result
  },
  streamCompression: async (sceneId: string, onEvent: (event: StreamEvent) => void) => {
    let result: StreamEvent['compression'] | undefined
    await streamPropose('/prose-review/compression', { scene_id: sceneId }, (event) => {
      onEvent(event)
      if (event.kind === 'done') result = event.compression
    })
    if (!result) throw new ApiError(502, 'Compression finished without a result payload')
    return result
  },
  rejectProseProposal: (id: string) =>
    post<{ ok: boolean; proposal: ProseReviewProposal }>(`/prose-review/${id}/reject`),

  // Person and tense are the author's call, so every novelization route takes a
  // partial voice: the fields given change, the rest are inherited.
  novelizePlan: (
    from: number,
    to: number,
    voice: Partial<Voice> = {},
    opts: { max_span_chars?: number } = {},
  ) => {
    const q = new URLSearchParams({ from: String(from), to: String(to) })
    if (voice.person) q.set('person', voice.person)
    if (voice.tense) q.set('tense', voice.tense)
    if (voice.focal !== undefined) q.set('focal', voice.focal)
    if (opts.max_span_chars != null) q.set('max_span_chars', String(opts.max_span_chars))
    return get<NovelizePlan>(`/novelize/plan?${q}`)
  },
  novelize: (
    from: number,
    to: number,
    opts: Partial<Voice> & {
      regenerate?: boolean
      direction?: string
      max_span_chars?: number
    } = {},
  ) => post<NovelizeResult>('/novelize', { from, to, ...opts }),
  /** Streaming novelize with per-chunk phase progress. Prefer this in the UI. */
  streamNovelize: async (
    from: number,
    to: number,
    opts: Partial<Voice> & {
      regenerate?: boolean
      direction?: string
      max_span_chars?: number
    } = {},
    onEvent: (event: StreamEvent) => void,
  ): Promise<NovelizeResult> => {
    let result: NovelizeResult | null = null
    await streamPropose('/novelize', { from, to, ...opts }, (event) => {
      onEvent(event)
      if (event.kind === 'done' && event.novelize) {
        result = event.novelize
      }
    })
    if (!result) {
      throw new ApiError(502, 'novelize finished without a result payload')
    }
    return result
  },
  /** Batch novelize pending scenes (skips already filed). Streams progress. */
  streamNovelizeBatch: async (
    opts: Partial<Voice> & {
      max_scenes?: number
      max_span_chars?: number
      regenerate?: boolean
      stop_on_error?: boolean
      max_retries?: number
      continuity_chars?: number
    } = {},
    onEvent: (event: StreamEvent) => void,
  ): Promise<NovelizeBatchResult> => {
    let result: NovelizeBatchResult | null = null
    await streamPropose('/novelize/batch', { ...opts }, (event) => {
      onEvent(event)
      if (event.kind === 'done' && event.batch) {
        result = event.batch
      }
    })
    if (!result) {
      throw new ApiError(502, 'batch novelize finished without a result payload')
    }
    return result
  },
  setBookVoice: (voice: Partial<Voice>) =>
    post<{ ok: boolean; voice: Voice; voice_label: string }>(
      '/manuscript/voice',
      voice,
    ),
  setSceneVoice: (id: string, voice: Partial<Voice> | { follow_book: true }) =>
    post<DerivedScene>(`/scene/${id}/voice`, voice),

  // The transform half. Each operator proposes and writes nothing; the proposal
  // waits in one place, and accepting it is a separate call that takes a backup.
  proposal: () => get<Proposal>('/proposal'),
  acceptEdits: (allowFlagged = false) =>
    post<{ ok: boolean; backup: string; applied: number }>('/edits/commit', {
      allow_flagged: allowFlagged,
    }),
  acceptOneEdit: (index: number, allowFlagged = false) =>
    post<{ ok: boolean; backup: string; applied: number; remaining: number }>(
      '/edits/commit-one',
      { index, allow_flagged: allowFlagged },
    ),
  rejectEdits: () => post<{ ok: boolean }>('/edits/discard'),
  proposePatch: (patches: { msg_id: number; body: string }[]) =>
    post<{ ok: boolean; advisory?: boolean }>('/log/patch', { patches }),
  proposeRemove: (from: number, to: number, sweep = false) =>
    post<{ ok: boolean; advisory?: boolean }>('/remove', { from, to, sweep }),
  dropEdit: (msgId: number) =>
    post<{ ok: boolean; msg_id: number }>('/edits/drop', { msg_id: msgId }),
  dropEditAt: (index: number) =>
    post<{ ok: boolean; index: number }>('/edits/drop', { index }),
  sweepLast: () =>
    post<{ ok: boolean; report: SweepReport; report_path: string }>('/sweep/last'),
  weedSources: () => get<{
    ok: boolean
    sources: import('./types').WeedSource[]
    selected_sources: string[]
  }>('/weed/sources'),
  setWeedSources: (selected_sources: string[]) => post<{
    ok: boolean
    sources: import('./types').WeedSource[]
    selected_sources: string[]
  }>('/weed/sources', { selected_sources }),
  scanWeed: (body: {
    from?: number
    to?: number
    speaker?: string
    sources?: string[]
  } = {}) => {
    const params = new URLSearchParams()
    if (body.from != null) params.set('from', String(body.from))
    if (body.to != null) params.set('to', String(body.to))
    if (body.speaker) params.set('speaker', body.speaker)
    for (const source of body.sources ?? []) params.append('source', source)
    const q = params.toString()
    return get<{ ok: boolean } & WeedReport>(q ? `/weed?${q}` : '/weed')
  },
  scanStamps: (body: { from?: number; to?: number } = {}) => {
    const params = new URLSearchParams()
    if (body.from != null) params.set('from', String(body.from))
    if (body.to != null) params.set('to', String(body.to))
    const q = params.toString()
    return get<{ ok: boolean } & StampReport>(q ? `/stamps?${q}` : '/stamps')
  },

  fonts: () => get<FontRegistry>('/fonts'),
  setFontRole: (role: string, patch: Record<string, unknown>) =>
    post<{ ok: boolean; fonts: FontRegistry }>('/fonts/role', { role, ...patch }),
  addSystemFont: (family: string, licence: string) =>
    post<{ ok: boolean; fonts: FontRegistry }>('/fonts/system', { family, licence }),
  removeFont: (family: string) =>
    post<{ ok: boolean; fonts: FontRegistry }>('/fonts/remove', { family }),
  importFont: async (file: File, licence: string): Promise<FontRegistry> => {
    const params = new URLSearchParams({ name: file.name, licence })
    const res = await fetch(`/fonts/import?${params}`, {
      method: 'POST',
      body: await file.arrayBuffer(),
    })
    const body = (await res.json()) as { ok?: boolean; error?: string; fonts: FontRegistry }
    if (!res.ok) throw new ApiError(res.status, body.error ?? 'font import failed')
    return body.fonts
  },

  // Bulk, even for one message: the page always annotates a span of turns, and
  // /attribution/set takes a single msg_id.
  setVoice: (msgIds: number[], voice: string, mode: string) =>
    post<{ ok: boolean; applied: number; skipped: number }>(
      '/attribution/bulk-set',
      { msg_ids: msgIds, voice, mode, reviewed: true },
    ),
}
