/** Word-level diff for proposal review.
 *
 * A proposal shows a whole paragraph before and after, and the edit is often
 * three words somewhere in the middle. This finds which stretches of each side
 * actually differ so the review can underline them. Whitespace is kept as its
 * own token so the ranges line up with the original string exactly.
 */

export type Range = [start: number, end: number]

// Past this many token pairs the table gets expensive; fall back to trimming
// the shared head and tail, which still isolates a single contiguous edit.
const MAX_CELLS = 1_500_000

function tokenize(text: string): string[] {
  return text.match(/\s+|[^\s]+/g) ?? []
}

function offsets(tokens: string[]): number[] {
  const out = [0]
  for (const token of tokens) out.push(out[out.length - 1] + token.length)
  return out
}

/** Merge touching ranges and let a lone space between two changes join them. */
function coalesce(ranges: Range[], text: string): Range[] {
  const out: Range[] = []
  for (const range of ranges) {
    const prev = out[out.length - 1]
    if (prev && /^\s*$/.test(text.slice(prev[1], range[0]))) prev[1] = range[1]
    else out.push([...range])
  }
  // A change that is only whitespace (a double space fixed) still deserves a
  // visible mark, so it is kept; but trim whitespace edges off word changes.
  return out.map(([start, end]) => {
    const slice = text.slice(start, end)
    if (!slice.trim()) return [start, end] as Range
    const lead = slice.length - slice.trimStart().length
    const trail = slice.length - slice.trimEnd().length
    return [start + lead, end - trail] as Range
  })
}

function trimDiff(a: string, b: string): { before: Range[]; after: Range[] } {
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let tail = 0
  while (
    tail < a.length - head && tail < b.length - head
    && a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) tail++
  return {
    before: head < a.length - tail ? [[head, a.length - tail]] : [],
    after: head < b.length - tail ? [[head, b.length - tail]] : [],
  }
}

/** Character ranges in `before` that were removed or replaced, and in `after`
 *  that were inserted. */
export function wordDiff(before: string, after: string): { before: Range[]; after: Range[] } {
  if (before === after) return { before: [], after: [] }
  const a = tokenize(before)
  const b = tokenize(after)
  if (a.length * b.length > MAX_CELLS) return trimDiff(before, after)

  const cols = b.length + 1
  const lcs = new Uint32Array((a.length + 1) * cols)
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * cols + j] = a[i] === b[j]
        ? lcs[(i + 1) * cols + j + 1] + 1
        : Math.max(lcs[(i + 1) * cols + j], lcs[i * cols + j + 1])
    }
  }

  const aAt = offsets(a)
  const bAt = offsets(b)
  const removed: Range[] = []
  const added: Range[] = []
  let i = 0
  let j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      i++
      j++
    } else if (j < b.length && (i === a.length || lcs[i * cols + j + 1] >= lcs[(i + 1) * cols + j])) {
      added.push([bAt[j], bAt[j + 1]])
      j++
    } else {
      removed.push([aAt[i], aAt[i + 1]])
      i++
    }
  }
  return { before: coalesce(removed, before), after: coalesce(added, after) }
}
