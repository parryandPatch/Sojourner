/**
 * How many genuinely different options the solver actually produced.
 *
 * The planner always returns three options, but it does not always return three
 * *different* ones. After a disruption it re-opens only the affected missions, and a
 * single asset fault can leave a single decision — at which point all three objectives
 * genuinely pick the same answer and only one plan exists.
 *
 * Showing that plainly is deliberate. The alternative — perturbing a plan until it looks
 * different — would produce a card labelled STABILITY FIRST that embodies an objective
 * nobody optimised, and a Commander would be weighing a fabricated trade-off. This
 * component says what happened instead.
 */
import { Badge } from './ui'
import type { OptionDistinctness } from '../types/api'

export function OptionDistinctnessNote({
  distinctness,
  optionsShown,
}: {
  distinctness: OptionDistinctness
  /** How many options were returned, normally 3. */
  optionsShown: number
}) {
  const { distinct_variants, duplicate_options, free_mission_count, distinct_options_note } =
    distinctness

  return (
    <div
      className="mt-3 rounded border border-edge bg-surface-sunken px-3 py-2"
      data-testid="option-distinctness"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
        <span className="tabular">
          distinct variants <strong>{distinct_variants}</strong> of {optionsShown}
        </span>
        <span className="tabular">
          free missions <strong>{free_mission_count}</strong>
        </span>
        {duplicate_options.map((group) => (
          <Badge key={group.join('|')} tone="caution">
            {group.join(' = ')}
          </Badge>
        ))}
      </div>

      <p
        className={`mt-1.5 text-[11px] leading-relaxed ${
          distinct_variants >= optionsShown ? 'text-ink-muted' : 'text-caution'
        }`}
      >
        {distinct_options_note}
      </p>

      {duplicate_options.map((group) => (
        <p key={`why-${group.join('|')}`} className="mt-1 text-[11px] leading-relaxed text-ink-faint">
          <strong className="text-ink-muted">{group.join(' and ')}</strong> are the same
          assignments. Only {free_mission_count} mission
          {free_mission_count === 1 ? ' was' : 's were'} free to be re-decided, so the three
          objectives had nothing left to disagree about.
        </p>
      ))}
    </div>
  )
}
