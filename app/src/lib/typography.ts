/** Which face the visible layer actually gets.
 *
 * A face per layer is an option, not a prerequisite: pick one for the page and
 * every layer follows it until you deliberately give the log its own. The
 * alternative — each role standing alone — means choosing a font and watching
 * nothing happen because the layer on screen reads a different, unset role.
 */

import type { FontRegistry, Layer, RoleSetting } from '../types'

export interface PageType {
  family: string
  size: number
  lineHeight: number
  measure: number
  tracking: number
  /** The role this came from, for the picker to open on the right tab. */
  role: string
  /** True when the layer has no face of its own and is following `page`. */
  inherited: boolean
}

const DEFAULTS: PageType = {
  family: 'Courier Prime',
  size: 16,
  lineHeight: 1.75,
  measure: 68,
  tracking: 0,
  role: 'page',
  inherited: false,
}

export function roleForLayer(layer: Layer): string {
  return layer === 'log' ? 'log' : 'page'
}

export function resolveType(fonts: FontRegistry | null, layer: Layer): PageType {
  const roles = fonts?.roles ?? {}
  const role = roleForLayer(layer)
  const own: RoleSetting | undefined = roles[role]
  const base: RoleSetting | undefined = roles.page

  // Zero and empty string both mean "not set", so each property falls back on
  // its own: a log role that sets only a measure still inherits the page face.
  const text = (a?: string, b?: string) => a || b || ''
  const num = (a?: number, b?: number) => a || b || 0

  return {
    family: text(own?.family, base?.family) || DEFAULTS.family,
    size: num(own?.size, base?.size) || DEFAULTS.size,
    lineHeight: num(own?.line_height, base?.line_height) || DEFAULTS.lineHeight,
    measure: num(own?.measure, base?.measure) || DEFAULTS.measure,
    tracking: num(own?.letter_spacing, base?.letter_spacing) || DEFAULTS.tracking,
    role,
    inherited: role !== 'page' && !own?.family && Boolean(base?.family),
  }
}
