/**
 * Application shell.
 *
 * Six screens, one shared header. The two safety labels and the role selector live here so
 * they are present on every screen without any screen having to remember them.
 *
 * There is no router library: the prototype has six destinations and a `useState` is the
 * smallest thing that can hold one.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, setRole } from './api/client'
import { SafetyBanner } from './components/SafetyBanner'
import { useAsync } from './hooks/useAsync'
import { useLiveFeed } from './hooks/useLiveFeed'
import { RoleContext, useRole } from './hooks/useRole'
import type { Role, RoleCapabilities } from './types/api'
import { AssetReadinessScreen } from './pages/AssetReadinessScreen'
import { CommandDeck } from './pages/CommandDeck'
import { DecisionTrailScreen } from './pages/DecisionTrailScreen'
import { DisruptionCentre } from './pages/DisruptionCentre'
import { PlanningScreen } from './pages/PlanningScreen'
import { WhatIfLab } from './pages/WhatIfLab'

export type ScreenId =
  | 'deck'
  | 'planning'
  | 'disruption'
  | 'whatif'
  | 'trail'
  | 'readiness'

export const SCREENS: Array<{ id: ScreenId; label: string; hint: string }> = [
  { id: 'deck', label: 'A · Command deck', hint: 'KPIs, map, timeline, live alerts' },
  { id: 'planning', label: 'B · Planning', hint: 'Three trade-off options' },
  { id: 'disruption', label: 'C · Disruption centre', hint: 'Inject, review, approve' },
  { id: 'whatif', label: 'D · What-if lab', hint: 'Sandboxed experiment' },
  { id: 'trail', label: 'E · Decision trail', hint: 'Ledger and replay' },
  { id: 'readiness', label: 'F · Asset readiness', hint: 'Synthetic readiness view' },
]

/**
 * A monotonically increasing counter every screen watches. Any mutation (plan generated,
 * event injected, proposal approved, what-if run) bumps it, and the screens reload. This
 * is deliberately coarse: with six screens and a local SQLite file, a global refresh tick
 * is simpler and less bug-prone than per-screen cache invalidation.
 */
export type RefreshTick = number

export default function App() {
  const [screen, setScreen] = useState<ScreenId>('deck')
  const [role, setActingRole] = useState<Role>('OBSERVER')
  const [tick, setTick] = useState<RefreshTick>(0)

  const bump = useCallback(() => setTick((value) => value + 1), [])

  // Role capabilities come from the server's own matrix so the UI can never advertise a
  // permission the API will refuse.
  const capabilities = useAsync(
    () => api.overview(),
    [role],
  )
  const roleMatrix = capabilities.data?.role_matrix ?? null

  useEffect(() => {
    setRole(role)
  }, [role])

  const live = useLiveFeed(bump)

  const roleValue = useMemo<{ role: Role; setRole: (next: Role) => void; capabilities: RoleCapabilities | null }>(
    () => ({
      role,
      setRole: setActingRole,
      capabilities: roleMatrix ? (roleMatrix[role] ?? null) : null,
    }),
    [role, roleMatrix],
  )

  return (
    <RoleContext.Provider value={roleValue}>
      <div className="flex h-full min-h-screen flex-col bg-surface text-ink">
        <SafetyBanner />

        <header className="border-b border-edge bg-surface-raised">
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-2">
            <div className="flex items-baseline gap-3">
              <h1 className="text-base font-bold tracking-[0.2em]">SOJOURNER</h1>
              <p className="text-[11px] text-ink-faint">
                air-operations decision support · offline prototype ·{' '}
                {capabilities.data?.scenario_name ?? 'loading scenario…'}
              </p>
            </div>
            <ClockControl tick={tick} onChanged={bump} />
          </div>

          <nav className="flex flex-wrap gap-1 px-2 pb-1" aria-label="Screens">
            {SCREENS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setScreen(entry.id)}
                title={entry.hint}
                aria-current={screen === entry.id ? 'page' : undefined}
                data-testid={`nav-${entry.id}`}
                className={`rounded-t px-3 py-1.5 text-xs font-semibold transition-colors ${
                  screen === entry.id
                    ? 'bg-surface-sunken text-accent shadow-[inset_0_-2px_0_0_#38bdf8]'
                    : 'text-ink-faint hover:text-ink'
                }`}
              >
                {entry.label}
              </button>
            ))}
          </nav>
        </header>

        <main className="flex-1 overflow-y-auto px-4 py-4">
          {screen === 'deck' && <CommandDeck tick={tick} onNavigate={setScreen} />}
          {screen === 'planning' && <PlanningScreen tick={tick} onChanged={bump} />}
          {screen === 'disruption' && <DisruptionCentre tick={tick} onChanged={bump} />}
          {screen === 'whatif' && <WhatIfLab tick={tick} />}
          {screen === 'trail' && <DecisionTrailScreen tick={tick} />}
          {screen === 'readiness' && <AssetReadinessScreen tick={tick} />}
        </main>

        <footer className="border-t border-edge bg-surface-raised px-4 py-1.5">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-faint">
            <span className="inline-flex items-center gap-1.5">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  live.connection === 'open'
                    ? 'live-dot bg-accent'
                    : live.connection === 'connecting'
                      ? 'bg-caution'
                      : 'bg-ink-faint'
                }`}
              />
              live feed: {live.connection} · {live.messages.length} message(s) buffered
            </span>
            <span>
              live feed is notification-only — it cannot change state, and no control on any
              screen acts without a human.
            </span>
          </div>
        </footer>
      </div>
    </RoleContext.Provider>
  )
}

/**
 * Operational clock control.
 *
 * Advancing the clock is what freezes assignments whose takeoff has passed, which is the
 * mechanism the disruption screen relies on. It is a synthetic control: it only writes the
 * minute number.
 */
function ClockControl({ tick, onChanged }: { tick: RefreshTick; onChanged: () => void }) {
  const overview = useAsync(() => api.overview(), [tick])
  const { capabilities } = useRole()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const now = overview.data?.now_minute ?? 0
  const horizon = overview.data?.horizon_minutes ?? 0

  const move = useCallback(
    async (target: number) => {
      setBusy(true)
      setMessage(null)
      try {
        const result = await api.advanceClock(target)
        setMessage(result.note)
        onChanged()
      } catch (error) {
        setMessage(error instanceof Error ? error.message : 'Clock change failed.')
      } finally {
        setBusy(false)
      }
    },
    [onChanged],
  )

  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className="text-ink-faint">operational clock</span>
      <span className="tabular rounded border border-edge-strong bg-surface-sunken px-2 py-0.5 font-semibold">
        minute {now} / {horizon}
      </span>
      <span className="tabular text-ink-faint">{overview.data?.now_iso ?? ''}</span>
      <button
        type="button"
        disabled={busy || !capabilities?.can_write || now <= 0}
        onClick={() => void move(Math.max(0, now - 60))}
        className="rounded border border-edge-strong px-2 py-0.5 disabled:opacity-40"
        title="Rewind one hour of scenario minutes (synthetic)"
      >
        −60
      </button>
      <button
        type="button"
        disabled={busy || !capabilities?.can_write || now >= horizon}
        onClick={() => void move(Math.min(horizon, now + 60))}
        className="rounded border border-edge-strong px-2 py-0.5 disabled:opacity-40"
        title="Advance one hour of scenario minutes (synthetic)"
        data-testid="clock-forward"
      >
        +60
      </button>
      {message && <span className="text-ink-muted">{message}</span>}
    </div>
  )
}