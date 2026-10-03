/** Shapes the engine sends. Mirrors story_editor/views.py and the route payloads;
 *  where a field can be absent the type says so rather than pretending. */

export type Layer = 'log' | 'manuscript'
export type BeatStatus = 'written' | 'partial' | 'planned' | 'unknown' | 'stale'

export interface AuthoredBeat {
  label: string
  number: number
  letter: string
  title: string
  text: string
  section: string
  status: BeatStatus
  derived_beats: number[]
  /** Ids the audit named that the committed spine no longer contains. */
  missing_derived: number[]
  start: number | null
  end: number | null
  message_count: number
  /** msg_ids the retrieval-grounded audit cited for this beat, if any. */
  evidence?: number[]
  verdict_note?: string
}

export interface DerivedBeat {
  beat_id: number
  title: string
  justification: string
  start_msg_id: number
  end_msg_id: number
  span_label: string
  message_count: number
  kind?: string
}

export interface PendingSpine {
  n_beats: number
  n_scenes: number
  coverage_ok: boolean | null
  beats: {
    beat_id: number
    title: string
    span_label: string
    kind: string
  }[]
  critique_notes: string[]
}

export interface SpineView {
  authored: AuthoredBeat[]
  derived: DerivedBeat[]
  alignment: { checked?: string; result?: AlignmentResult } | null
  drift: string[]
  has_derived_spine: boolean
  unit_type: 'beat' | 'episode'
  authority: 'derived' | 'canon_locked' | string
  coverage: unknown
  /** Uncommitted proposal from derive — commit or discard before validate uses it. */
  pending?: PendingSpine | null
  /** How far the committed spine reaches, and how much log sits past it. */
  tail: {
    last_mapped: number | null
    log_end: number | null
    unmapped: number
    derived_at: string
  }
  counts: {
    authored: number
    derived: number
    written: number
    planned: number
    partial: number
    stale: number
  }
  checked?: string
}

export interface AlignmentResult {
  alignment?: {
    derived: number
    authored: string | number | null
    note?: string
    evidence?: number[]
  }[]
  absent?: (string | number)[]
  drift?: string[]
  coverage?: string
  beats?: {
    authored: string
    verdict: 'present' | 'partial' | 'absent' | string
    evidence?: number[]
    derived?: number[]
    note?: string
  }[]
}

export interface AskCitation {
  msg_id: number
  speaker: string
  preview: string
}

export interface AskResult {
  question: string
  answer: string
  citations: AskCitation[]
  as_of: { msg_id: number; label: string } | null
  mode: string
  hits_considered: number
}

export interface AskOptions {
  log_tip: number
  derived: { value: string; label: string; msg_id: number }[]
  authored: { value: string; label: string; msg_id: number }[]
}

export interface SceneRow {
  scene_id: number
  kind: 'scene' | 'interlude' | 'frontmatter'
  start: number
  end: number
  message_count: number
  date: string | null
  time_start: string | null
  location: string
  stage: string | null
  speakers: string[]
  synopsis: string
  beat_id: number | null
  beat_title: string
  episode_id?: number | null
  episode_title?: string
  manuscript: { id: string; status: string } | null
  manuscript_parts?: { id: string; status: string; start: number; end: number }[]
}

export interface PageMessage {
  msg_id: number
  uid: string
  speaker: string
  role: 'user' | 'char' | 'system'
  text: string
  prose: string
  header: boolean
  interlude: boolean
  voice: string
  mode: string
  manuscript_scene: string | null
}

export interface LogSpan {
  messages: PageMessage[]
  start: number
  end: number
  total: number
  truncated: boolean
}

/** One verb from GET /layers. `layers` is where the design allows it; `runs_on`
 *  is where an operator has actually been written, and `pending` is the engine's
 *  own sentence about the difference. */
export interface OpInfo {
  layers: Layer[]
  what: string
  runs_on: Layer[]
  pending: string
}

export interface LayerMap {
  layers: Layer[]
  syncable: Layer[]
  ops: Record<string, OpInfo>
}

export interface DiffHunk {
  kind: 'same' | 'del' | 'ins'
  text: string
}

/** One message's proposed change, as GET /proposal renders it. The header is
 *  held out of the diff because the operators pass it through verbatim. */
export interface EditDiff {
  index?: number
  msg_id: number
  speaker: string
  kind: 'replace' | 'inject' | 'remove'
  header: string
  header_preserved: boolean
  before: string
  after: string
  hunks: DiffHunk[]
  flags: string[]
  hard_flagged: boolean
}

/** Whatever is waiting for a verdict. `kind` is null when nothing is. */
export interface Proposal {
  kind: 'edits' | null
  layer?: Layer
  op?: string
  operator?: string
  note?: string
  locator?: string
  log?: string
  created?: string
  stage?: string
  advisory?: boolean
  edits: EditDiff[]
  flagged?: number
  hard_flagged?: number
  meta?: WeedMeta & { sweep?: boolean; count?: number; warning?: string }
}

export interface SweepHit {
  msg_id: number
  speaker: string
  excerpt: string
  verdict: string
  reason: string
}

export interface WeedHit {
  msg_id: number
  speaker: string
  sentence: string
  phrase_id: string
  snippet: string
  start: number
  end: number
  source_id?: string
}

export interface WeedReport {
  locator: string
  from: number
  to: number
  hits: WeedHit[]
  phrases: string[]
  hit_count: number
  message_count: number
  selected_sources: string[]
}

export interface WeedSource {
  id: string
  kind: 'human' | 'model' | 'system' | string
  speaker: string
  label: string
  message_count: number
  selected: boolean
}

export interface StampRow {
  uid: string
  msg_id: number
  speaker: string
  role: string
  date: string | null
  time: string | null
  location: string | null
  source: string
  complete: boolean
  has_lead: boolean
  will_propose: boolean
}

export interface StampReport {
  locator: string
  from: number
  to: number
  count: number
  complete: number
  header: number
  inherited: number
  inferred: number
  missing: number
  propose_count: number
  propose: StampRow[]
}

export interface WeedMeta {
  phrases?: string[]
  hit_count?: number
  mechanical?: number
  rewritten?: number
  review_total?: number
  review_accepted?: number
  review_rejected?: number
}

export interface SweepReport {
  changed_from: number
  changed_to: number
  sweep_from: number
  operator: string
  note: string
  hits: SweepHit[]
  clean_count: number
  verdict: string
  created: string
}

export interface RoleSetting {
  family: string
  size: number
  line_height: number
  measure: number
  letter_spacing: number
}

export interface UserFace {
  family: string
  kind: 'bundled' | 'system' | 'imported'
  licence: string
  file: string
  style: string
  weight: string
  added: string
  available: boolean | null
  url: string
}

export interface FontRegistry {
  roles: Record<string, RoleSetting>
  faces: UserFace[]
  dir: string
  updated: string
  kinds: Record<string, string>
}

export type Person = 'first' | 'second' | 'close_third' | 'omniscient'
export type Tense = 'past' | 'present'
export type SceneStatus = 'draft' | 'approved' | 'rejected'

export interface Voice {
  person: Person
  tense: Tense
  focal?: string
}

export interface ManuscriptView {
  layer: Layer
  log: string
  updated: string
  stats: Record<string, number>
  drifted: number
  orchestration_total?: number
  orchestration_pending?: number
  voice: Voice
  voice_label: string
  persons: Record<Person, string>
  tenses: Record<Tense, string>
  scenes: {
    id: string
    title: string
    status: SceneStatus
    start: number
    end: number
    blocks: number
    words: number
    drift: { scene_id: string; kind: string; detail: string }
    pinned?: boolean
    voice: Voice
  }[]
}

export interface NovelizeBatchItem {
  from: number
  to: number
  title: string
  status: string
  scene_id?: string
  error?: string
  warnings?: string[]
  attempts: number
  elapsed_seconds: number
  log_scene_id?: number
  episode_id?: number
  episode_title?: string
}

export interface NovelizeBatchResult {
  ok: boolean
  novelized: NovelizeBatchItem[]
  skipped: NovelizeBatchItem[]
  failed: NovelizeBatchItem[]
  counts: { novelized: number; skipped: number; failed: number }
  campaign?: {
    path: string
    status: string
    backup?: string
    assembled?: string
  }
}

export interface DerivedBlock {
  id: string
  kind: string
  text: string
  src?: string[]
  edited?: boolean
  note?: string
}

/** One derived scene, as GET /scene/{id} returns it. */
export interface DerivedScene {
  id: string
  layer: Layer
  status: SceneStatus
  title: string
  voice: Voice | null
  voice_label: string
  source: { from_uid: string; to_uid: string; start: number; end: number }
  drift: { scene_id: string; kind: string; detail: string }
  pinned?: boolean
  generated: string
  model: string
  notes: string
  blocks: DerivedBlock[]
  text: string
  words: number
}

export interface ProseReviewChange {
  block_id: string
  before: string
  after: string
}

export interface ProseReviewProposal {
  id: string
  kind: 'compression' | 'syntax' | 'rhythm'
  scene_id: string
  changes: ProseReviewChange[]
  reason: string
  risks: string[]
  status: 'pending' | 'committed' | 'rejected' | 'stale'
  created: string
  decided: string
  backup: string
  original_hash: string
  before: string
  after: string
  before_words: number
  after_words: number
  word_delta: number
}

export interface ProseReviewCounts {
  pending: number
  committed: number
  rejected: number
  stale: number
  verify: number
}

export interface ProseReviewSummary {
  total: number
  totals: ProseReviewCounts
  scenes: Record<string, ProseReviewCounts>
}

/** Log-side of a novelized scene — left column of a Queue PR card. */
export interface LogSceneView {
  id: string
  layer: 'log'
  source: { from_uid: string; to_uid: string; start: number; end: number }
  messages: {
    msg_id: number
    uid: string
    speaker: string
    role: string
    text: string
  }[]
  text: string
}

/** One contiguous pack under the one-pass ceiling — GET /novelize/plan. */
export interface NovelizeChunk {
  from: number
  to: number
  source_chars: number
  turns: number
}

/** What a novelization pass would do — GET /novelize/plan. */
export interface NovelizePlan {
  from: number
  to: number
  title: string
  turns: number
  source_chars: number
  max_span_chars: number
  too_long: boolean
  /** How many passes the chunked runner would take (0 if unchunkable). */
  chunk_count: number
  chunks: NovelizeChunk[]
  /** True when a single turn exceeds the ceiling and cannot be split. */
  unchunkable: boolean
  voice: Voice
  voice_label: string
  book_voice: Voice
  persons: Record<Person, string>
  tenses: Record<Tense, string>
  focal_candidates: { name: string; turns: number }[]
  existing: {
    id: string
    status: SceneStatus
    generated: string
    voice: Voice
  } | null
}

export interface NovelizeResult {
  ok: boolean
  saved: boolean
  text: string
  voice: Voice
  voice_label: string
  turns: number
  paragraphs: number
  source_chars: number
  prose_chars: number
  unmarked_paragraphs: number
  unresolved_paragraphs: number
  model: string
  warnings: string[]
  scene?: DerivedScene
}

export interface ModelSettings {
  provider: 'local' | 'openrouter' | string
  model: string
  base_url: string
  local_port: number | null
  api_key_set: boolean
  presets: { creative: string; analyst: string }
  providers: string[]
  defaults: {
    local: { model: string; base_url: string }
    openrouter: { model: string; base_url: string }
  }
}

export interface EngineStatus {
  api_version: string
  home_dir: string
  log: string
  message_count: number
  model_reachable: boolean
  model_url: string
  model_provider?: string
  model_api_key_set?: boolean
  model_default?: string
  model_presets?: { creative: string; analyst: string }
  model?: ModelSettings
  history_entries: number
  pending_edits: number
  pending_kind: 'edits' | null
  pending_operator: string | null
  /** Sweep reads the last commit rather than a selection, so it can only be
   *  offered once something has been committed for it to follow. */
  sweep_ready: boolean
  sweep_note: string | null
  sweep_from: number | null
}

export interface SyncStatus {
  kind: 'in_sync' | 'working_ahead' | 'st_ahead' | 'diverged' | 'st_missing' | 'working_missing'
  working: string
  st: string
  st_exists: boolean
  working_count: number
  st_count: number
  in_sync: boolean
  changed_text_count?: number
  changed_message_count?: number
  changed_msg_ids?: number[]
}

export interface SyncPushResult {
  ok: boolean
  source: string
  dest: string
  messages: number
  written: boolean
  backup: string | null
}

export interface HistoryEdit {
  msg_id: number
  speaker: string
  kind: 'replace' | 'inject' | 'remove' | string
  before: string
  after: string
  flags: string[]
  block_id?: string
}

export interface HistoryEntry {
  id: number
  created: string
  log: string
  event: 'commit' | 'undo' | string
  operator: string
  note: string
  locator: string
  backup: string
  changed_from: number | null
  changed_to: number | null
  edits: HistoryEdit[]
  layer: 'log' | 'manuscript' | string
  scene_id: string
  scene_title: string
}

export interface DriveManifestHeader {
  id: string
  parent: string
  machine: string
  pushed_at: string
  file_count: number
}

export interface DriveStatus {
  kind:
    | 'in_sync' | 'local_ahead' | 'drive_ahead' | 'diverged' | 'drive_empty'
    | 'unlinked' | 'no_rclone' | 'no_remote' | 'drive_error'
  detail: string
  remote: string
  machine: string
  local_changes: string[]
  drive_changes: string[]
  drive: DriveManifestHeader | null
  base: DriveManifestHeader | null
  can_push: boolean
  can_pull: boolean
}

export interface DrivePullResult {
  ok: boolean
  status: DriveStatus
  replaced: string[]
  removed: string[]
  backup: string
}

export interface CodeStatus {
  kind: 'up_to_date' | 'behind' | 'ahead' | 'diverged' | 'no_repo' | 'error'
  detail: string
  branch: string
  head: string
  remote_head: string
  behind: number
  ahead: number
  dirty: string[]
  incoming: string[]
  fetched: boolean
  can_update: boolean
  can_push: boolean
}

export interface CodeUpdateResult {
  ok: boolean
  status: CodeStatus
  updated: boolean
  changed: string[]
  dependencies_changed: boolean
  restarting: boolean
}

export interface DriveProgress {
  bytes: number | null
  totalBytes: number | null
  transfers: number | null
  totalTransfers: number | null
  checks: number | null
  totalChecks: number | null
  speed: number | null
  eta: number | null
  errors: number | null
  current: string[]
}

export interface DriveJob {
  running: boolean
  direction: 'push' | 'pull' | ''
  phase: string
  progress: DriveProgress | null
  result: (Partial<DrivePullResult> & { status?: DriveStatus }) | null
  error: string
  refused: boolean
  started: string
  finished: string
}

export interface ProjectEntry {
  id: string
  home: string
  title: string
  exists: boolean
  open: boolean
}

export interface ProjectsView {
  current: { id: string; title: string; home: string; source: string; warning: string }
  active: string | null
  projects: ProjectEntry[]
  registry: string
  registry_error: string
  projects_dir: string
  env_override: boolean
}

export interface StagedUpload {
  token: string
  bundle: {
    ok: boolean
    project_id: string
    title: string
    files: number
    bytes: number
    created: string | null
    machine: string | null
    external: { role: string; original: string; path: string }[]
    not_bundled: { role: string; original: string; reason: string }[]
  }
  suggested_id: string
  projects_dir: string
  target: string
}

export interface LoadedProject {
  home: string
  project_id: string
  title: string
  files: number
  rewrites: string[]
  registered: boolean
}

export interface CastCounts {
  voiced: number
  pov: number
  mentioned: number
}

export interface CastRow {
  key: string
  name: string
  label: string
  role: string
  aliases: string[]
  origin: string
  counts: CastCounts
  first: number | null
  last: number | null
  has_portrait: boolean
  has_dossier: boolean
}

export interface CastView {
  characters: CastRow[]
  messages: number
}

export interface CastSpan {
  start: number
  end: number
  jump: number
  how: 'voiced' | 'pov' | 'mentioned'
  scene: string
  counts: Partial<CastCounts>
}

export interface CastMember {
  key: string
  name: string
  label: string
  role: string
  aliases: string[]
  origin: string
  first_scene: string
  status: string
  voice_rules: string[]
  forbidden_phrasings: string[]
  canonical_facts: string[]
  relationship_notes: Record<string, string>
  counts: CastCounts
  spans: CastSpan[]
  has_portrait: boolean
  dossier: { id: string; kind: string; text: string }[]
}

export interface PlayRow {
  uid: string
  name: string
  is_user: boolean
  text: string
  kind: string
  scene: string
  scene_title: string
  swipes: number
  swipe_id: number
}

export interface PlayView {
  configured: boolean
  home: string
  name: string
  status: {
    scene: string
    title: string
    question: string
    rung: number
    rungs: number
    moves: number
    dwell: number
    pov: string
    pov_name: string
    stage: string[]
    mystery: string
    cost: number
    cost_today: number
    pending: { type: string; summary?: string } | null
  }
  rows: PlayRow[]
  cast: { id: string; name: string }[]
  not_in_log: number
  can_undo: boolean
  busy: boolean
}
