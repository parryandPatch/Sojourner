/**
 * The two persistent safety labels.
 *
 * These are rendered on every screen, above the fold, and never inside a dismissible
 * container. The spec requires both strings to be visible at all times, so they are
 * duplicated in App rather than passed down as props.
 */
import { ROLES, useRole } from '../hooks/useRole'
import { ADVISORY_NOTICE, SYNTHETIC_NOTICE } from '../types/api'

export function SafetyBanner() {
  const { role, setRole } = useRole()

  return (
    <div className="safety-banner border-b border-caution/40 px-4 py-2">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span
            className="rounded-sm bg-caution px-2 py-0.5 text-xs font-bold tracking-widest text-slate-950"
            data-testid="synthetic-notice"
          >
            {SYNTHETIC_NOTICE}
          </span>
          <span
            className="rounded-sm border border-caution/60 px-2 py-0.5 text-xs font-semibold tracking-wide text-caution"
            data-testid="advisory-notice"
          >
            {ADVISORY_NOTICE}
          </span>
        </div>

        <label className="flex items-center gap-2 text-xs text-ink-muted">
          <span className="font-semibold uppercase tracking-wider">Acting role</span>
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as typeof role)}
            data-testid="role-selector"
            className="rounded border border-edge-strong bg-surface-sunken px-2 py-1 text-xs text-ink focus:border-accent focus:outline-none"
          >
            {ROLES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <span className="hidden text-ink-faint lg:inline">
            (demo switch only — the API enforces this server-side)
          </span>
        </label>
      </div>
    </div>
  )
}
