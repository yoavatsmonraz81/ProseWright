/** The bundled catalogue, and the honesty the picker owes you.
 *
 * Each face states its licence beside its name, because the distinction between
 * "ships with the editor" and "yours, on this machine only" belongs at the point
 * of choosing rather than in a document nobody re-reads.
 *
 * `italic` is not decoration: the prose leans on italics constantly, and a face
 * without one gets a browser-synthesised slant that looks like a photocopy of a
 * photocopy. Faces lacking it are offered for accents and never for the page.
 */

export interface Face {
  family: string
  licence: string
  italic: boolean
  note: string
  roles: ('page' | 'log' | 'ui' | 'diff' | 'accent')[]
}

export const BUNDLED: Face[] = [
  {
    family: 'Courier Prime',
    licence: 'OFL 1.1',
    italic: true,
    note: 'built for screenplay-length reading; real italic and bold',
    roles: ['page', 'log', 'diff'],
  },
  {
    family: 'TT2020 Base',
    licence: 'OFL 1.1',
    italic: true,
    note: 'randomised glyph variants — a repeated letter never looks stamped twice',
    roles: ['page', 'log'],
  },
  {
    family: 'Crimson Pro',
    licence: 'OFL 1.1',
    italic: true,
    note: 'for when the manuscript should read as a book rather than a draft',
    roles: ['page', 'ui'],
  },
  {
    family: 'Special Elite',
    licence: 'Apache 2.0',
    italic: false,
    note: 'distressed display — stamps and labels, never body text',
    roles: ['accent'],
  },
]

/** Faces worth wanting that we cannot ship. Named here so the picker can point
 *  at them: install through the OS, then add the family name below. */
export const SUGGESTED_SYSTEM: { family: string; licence: string }[] = [
  { family: 'Traveling Typewriter', licence: 'personal use — donationware' },
  { family: 'Maquina de Escribir', licence: 'personal use — commercial via MyFonts' },
  { family: 'Bohemian Typewriter', licence: 'personal use only' },
  { family: 'CMU Typewriter Text', licence: 'OFL 1.1 — if installed' },
]

/** Characters this manuscript actually uses. A 230-glyph display face will fall
 *  back per character and leave mismatched dashes mid-page, so probe for the
 *  real set rather than a pangram. */
export const COVERAGE_PROBE = '— … “ ” ‘ ’ ø æ å Ø Æ Å é ü'

/** Whether a family resolved, by measuring it against a name that cannot exist.
 *  Browsers refuse to enumerate installed fonts without a Chromium-only API, so
 *  measurement is the honest answer available to any browser. */
export function resolves(family: string): boolean {
  if (!family) return false
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) return false
  const sample = 'HAMBURGEFONTSIV 0123456789'
  const measure = (spec: string) => {
    ctx.font = spec
    return ctx.measureText(sample).width
  }
  const sentinel = measure('64px "nonexistent-face-sentinel"')
  const candidate = measure(`64px "${family}", "nonexistent-face-sentinel"`)
  return Math.abs(candidate - sentinel) > 0.5
}

/** Which probe characters the face lacks, by the same measuring trick per glyph. */
export function missingGlyphs(family: string): string[] {
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) return []
  const missing: string[] = []
  for (const ch of COVERAGE_PROBE.split(' ')) {
    if (!ch) continue
    ctx.font = '64px "nonexistent-face-sentinel"'
    const fallback = ctx.measureText(ch).width
    ctx.font = `64px "${family}", "nonexistent-face-sentinel"`
    const actual = ctx.measureText(ch).width
    if (Math.abs(actual - fallback) < 0.5) missing.push(ch)
  }
  return missing
}
