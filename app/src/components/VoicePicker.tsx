/** Person, tense, and whose head we are in.
 *
 * The two decisions novelization cannot avoid, so they are made in the open and
 * on the page rather than buried in a prompt. Emits partial changes: choosing a
 * tense must not silently reset the focal character.
 */

import type { Person, Tense, Voice } from '../types'

interface Props {
  voice: Voice
  persons: Record<string, string>
  tenses: Record<string, string>
  cast?: { name: string; turns: number }[]
  disabled?: boolean
  onChange: (patch: Partial<Voice>) => void
}

// Omniscient has no single vantage, so asking whose head it is in is meaningless.
const NEEDS_FOCAL: Person[] = ['first', 'second', 'close_third']

export function VoicePicker({
  voice,
  persons,
  tenses,
  cast = [],
  disabled,
  onChange,
}: Props) {
  const needsFocal = NEEDS_FOCAL.includes(voice.person)
  const names = cast.map((c) => c.name)
  const focal = voice.focal ?? ''
  const known = !focal || names.includes(focal)

  return (
    <div className="voice-picker">
      <label>
        <span>person</span>
        <select
          value={voice.person}
          disabled={disabled}
          onChange={(e) => onChange({ person: e.target.value as Person })}
        >
          {Object.entries(persons).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span>tense</span>
        <select
          value={voice.tense}
          disabled={disabled}
          onChange={(e) => onChange({ tense: e.target.value as Tense })}
        >
          {Object.entries(tenses).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>

      {needsFocal && (
        <label>
          <span>follows</span>
          <select
            value={known ? focal : '__other'}
            disabled={disabled}
            onChange={(e) => onChange({ focal: e.target.value })}
          >
            {focal === '' && <option value="">(the scene's own lead)</option>}
            {names.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            {!known && <option value={focal}>{focal}</option>}
          </select>
        </label>
      )}
    </div>
  )
}
