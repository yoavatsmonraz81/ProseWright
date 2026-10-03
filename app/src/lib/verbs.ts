/** The verb list, assembled from what the engine says it can do.
 *
 * `GET /layers` answers two questions — what is legal on a layer, and what has
 * actually been written — and this file answers a third that is nobody else's
 * business: which surface in this GUI asks for it. Keeping only the third here is
 * what lets the list stay honest about the first two, so a verb the engine has
 * not implemented is shown greyed with the engine's own sentence rather than
 * offered as a button that fails.
 */

import type { Layer, LayerMap } from '../types'

export type VerbState =
  | 'ready'
  | 'needs-selection'
  | 'waiting'
  | 'unavailable'
  | 'elsewhere'

export interface Verb {
  name: string
  what: string
  state: VerbState
  /** A plain sentence, for anything that is not simply ready. */
  reason: string
}

/** The verbs the composer runs, in the order they are offered. */
const COMPOSED: string[] = [
  'restyle',
  'retune',
  'weed',
  'copyedit',
  'stamps',
  'inject',
  'sweep',
]

/** Verbs that exist but are asked for elsewhere in the app. */
const ELSEWHERE: Record<string, string> = {
  novelize: 'The page offers this one, where a scene has no prose yet.',
  edit: 'Edit the prose in place on the page.',
  patch: 'The edit switch copies the scene out; you approve the diff.',
  annotate: 'Who-spoke is the selection bar’s other half.',
  remove: 'The selection bar offers remove, and remove with a downstream look.',
}

export interface VerbContext {
  /** Whether turns are selected on the page — most verbs need a span. */
  selection: boolean
  /** Whether a committed change exists for sweep to follow. */
  sweepReady: boolean
}

export function verbsFor(
  map: LayerMap | null,
  layer: Layer,
  ctx: VerbContext,
): Verb[] {
  if (!map) return []
  const legal = Object.entries(map.ops).filter(([, op]) => op.layers.includes(layer))
  legal.sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
  return legal.map(([name, op]) => {
    const base = { name, what: op.what }
    if (!op.runs_on.includes(layer)) {
      return { ...base, state: 'unavailable' as VerbState, reason: op.pending }
    }
    if (ELSEWHERE[name]) {
      return { ...base, state: 'elsewhere' as VerbState, reason: ELSEWHERE[name] }
    }
    if (!COMPOSED.includes(name)) {
      return {
        ...base,
        state: 'elsewhere' as VerbState,
        reason: 'The engine runs this one; the GUI has no control for it yet.',
      }
    }
    if (name === 'sweep') {
      return ctx.sweepReady
        ? { ...base, state: 'ready' as VerbState, reason: '' }
        : {
            ...base,
            state: 'waiting' as VerbState,
            reason:
              'Sweep reads the last committed change and looks downstream of it. ' +
              'Nothing has been committed for it to follow.',
          }
    }
    if (name === 'weed' || name === 'copyedit' || name === 'stamps') {
      return {
        ...base,
        state: 'ready' as VerbState,
        reason: '',
      }
    }
    if (!ctx.selection) {
      return {
        ...base,
        state: 'needs-selection' as VerbState,
        reason: 'Select the turns to change on the page first.',
      }
    }
    return { ...base, state: 'ready' as VerbState, reason: '' }
  })
}

function rank(name: string): number {
  const at = COMPOSED.indexOf(name)
  return at === -1 ? 100 : at
}
