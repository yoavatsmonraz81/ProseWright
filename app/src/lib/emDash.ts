/** Typing `--` in a prose box becomes an em dash, the way word processors do it.
 *
 * Only the pair just typed (immediately before the caret) is converted, so pasted
 * text and existing hyphens are left alone. A line made only of dashes — the
 * manuscript's `-------` scene separators — is never touched.
 */
export function withEmDash(el: HTMLTextAreaElement): string {
  const { value } = el
  const caret = el.selectionStart ?? value.length
  if (caret < 2 || value.slice(caret - 2, caret) !== '--') return value
  const lineStart = value.lastIndexOf('\n', caret - 1) + 1
  if (/^-+$/.test(value.slice(lineStart, caret))) return value
  const next = value.slice(0, caret - 2) + '—' + value.slice(caret)
  // React re-renders the controlled value and would drop the caret at the end.
  requestAnimationFrame(() => {
    el.selectionStart = el.selectionEnd = caret - 1
  })
  return next
}
