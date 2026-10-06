/**
 * Screen A — Command deck.
 *
 * The default view: KPIs, the schematic grid, the assignment timeline, and the live alert
 * feed. Everything here is read-only. The deck is where a reviewer lands first, so it also
 * states plainly that no plan is active until a Commander says so.
 */
import { api } from '../api/client'
import { GridMap, MapSummary } from '../components/GridMap'
import { Timeline } from '../components/Timeline'
import { Badge, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import type { RefreshTick, ScreenId } from '../App'

const TONE_CLASS: Record<string, string> = {
  neutral: 'border-edge',
  positive: 'border-emerald-500/40',
  warning: 'border-caution/40',
  critical: 'border-rose-500/50',
}

export function CommandDeck({
  tick,
  onNavigate,
}: {
  tick: RefreshTick
  onNavigate: (screen: ScreenId) => void
}) {
  const overview = useAsync(() => api.overview(), [tick])
  const map = useAsync(() => api.map(), [tick])

  if (overview.loading && !overview.data) return <Loading label="Loading command deck…" />
  if (overview.error && !overview.data) return <ErrorNote error={overview.error} />

  const data = overview.data
  if (!data) return <Empty>Command deck unavailable.</Empty>

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-7">
        {data.kpis.map((tile) => (
          <div
            key={tile.key}
            className={`rounded-lg border bg-surface-raised px-3 py-2 ${TONE_CLASS[tile.tone] ?? TONE_CLASS.neutral}`}
          >
            <p className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
              {tile.label}
            </p>
            <p className="tabular mt-0.5 truncate text-sm font-bold" title={tile.value}>
              {tile.value}
            </p>
            <p className="mt-0.5 text-[10px] leading-tight text-ink-faint">{tile.detail}</p>
          </div>
        ))}
      </div>

      {data.active_plan === null && (
        <div className="rounded-lg border border-caution/40 bg-caution/10 px-4 py-3 text-xs text-caution">
          <strong className="font-semibold">No plan is active.</strong> The plan set exists as
          drafts only. A Commander must approve a proposal before any of those versions takes
          effect — the system will not activate one on its own.{' '}
          <button
            type="button"
            className="underline underline-offset-2"
            onClick={() => onNavigate('planning')}
          >
            Open the planning screen
          </button>
          .
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <Card
          title="Neutral synthetic grid"
          subtitle="Fictional hubs, assets and mission areas. No real geography, coordinates, or units."
          actions={map.data ? <MapSummary data={map.data} /> : null}
        >
          {map.error ? (
            <ErrorNote error={map.error} />
          ) : map.data ? (
            <GridMap data={map.data} />
          ) : (
            <Loading />
          )}
        </Card>

        <div className="space-y-4">
          <Card title="Live alerts" subtitle="Readiness and synthetic restriction notices.">
            {data.readiness_alerts.length === 0 ? (
              <Empty>No readiness alerts in the seeded scenario.</Empty>
            ) : (
              <ul className="space-y-2">
                {data.readiness_alerts.map((alert) => (
                  <li
                    key={alert.asset_id}
                    className="rounded border border-edge bg-surface-sunken px-3 py-2 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <Badge tone={alert.severity === 'HIGH' ? 'bad' : 'caution'}>
                        {alert.severity}
                      </Badge>
                      <span className="tabular font-semibold">{alert.asset_id}</span>
                      <span className="text-ink-faint">{alert.label}</span>
                      <span className="tabular ml-auto text-ink-muted">
                        readiness {alert.readiness_probability.toFixed(2)}
                      </span>
                    </div>
                    <p className="mt-1 text-ink-muted">{alert.message}</p>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card
            title="Scenario events"
            subtitle="Most recent first. Events are synthetic and injected by the operator."
          >
            {data.recent_events.length === 0 ? (
              <Empty>No events yet. Inject one from the disruption centre.</Empty>
            ) : (
              <ul className="space-y-1.5">
                {data.recent_events.map((event) => (
                  <li key={event.id} className="border-l-2 border-edge-strong pl-2 text-xs">
                    <div className="flex items-baseline gap-2">
                      <span className="tabular font-semibold">{event.kind}</span>
                      <span className="text-ink-muted">{event.label}</span>
                      <span className="tabular ml-auto text-ink-faint">
                        min {event.occurred_minute}
                      </span>
                    </div>
                    <p className="tabular text-[10px] text-ink-faint">
                      {event.id} · {event.occurred_at} · source {event.source}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Scenario contents" subtitle="All counts refer to synthetic entities.">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
              {Object.entries(data.counts).map(([key, value]) => (
                <div key={key} className="flex items-baseline justify-between gap-2">
                  <dt className="text-ink-faint">{key.replace(/_/g, ' ')}</dt>
                  <dd className="tabular font-semibold">{value}</dd>
                </div>
              ))}
            </dl>
          </Card>
        </div>
      </div>

      <Card
        title="Assignment timeline"
        subtitle={`Active plan ${data.active_plan?.id ?? '—'} · minutes are scenario-relative.`}
      >
        <Timeline
          rows={data.timeline}
          nowMinute={data.now_minute}
          horizonMinutes={data.horizon_minutes}
        />
      </Card>

      <p className="text-[11px] leading-relaxed text-ink-faint">
        Risk values everywhere in this prototype come from a transparent three-component
        synthetic score (hazard, weather, readiness), not from a validated model. The planner
        is advisory: it proposes options, and a human Commander decides. Nothing on this
        screen can change the active plan.
      </p>
    </div>
  )
}