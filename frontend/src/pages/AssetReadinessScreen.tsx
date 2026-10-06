/**
 * Screen F — Asset readiness.
 *
 * Readiness is a transparent synthetic heuristic, not a trained or validated model. The
 * formula is printed on the card, every band is a word as well as a colour, and the
 * uncertainty interval is shown alongside the point estimate so the number cannot be
 * mistaken for a measurement.
 */
import { useMemo, useState } from 'react'

import { api } from '../api/client'
import { Badge, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import type { AssetRow } from '../types/api'
import type { RefreshTick } from '../App'

type Band = 'low' | 'mid' | 'high'

const BAND_TONE: Record<string, 'bad' | 'caution' | 'good'> = {
  LOW: 'bad',
  MEDIUM: 'caution',
  HIGH: 'good',
}

const STATUS_TONE: Record<string, 'good' | 'caution' | 'bad'> = {
  AVAILABLE: 'good',
  LIMITED: 'caution',
  UNAVAILABLE: 'bad',
}

export function AssetReadinessScreen({ tick }: { tick: RefreshTick }) {
  const readiness = useAsync(() => api.readiness(), [tick])
  const [filter, setFilter] = useState<'ALL' | 'AVAILABLE' | 'LIMITED' | 'UNAVAILABLE'>('ALL')
  const [sort, setSort] = useState<'readiness' | 'id' | 'maintenance'>('readiness')

  const rows = useMemo(() => {
    const all = readiness.data?.assets ?? []
    const filtered = filter === 'ALL' ? all : all.filter((asset) => asset.status === filter)
    const sorted = [...filtered]
    sorted.sort((a, b) => {
      if (sort === 'id') return a.id.localeCompare(b.id)
      if (sort === 'maintenance') return b.maintenance_hours_since - a.maintenance_hours_since
      return a.readiness_probability - b.readiness_probability
    })
    return sorted
  }, [readiness.data, filter, sort])

  if (readiness.loading && !readiness.data) return <Loading label="Loading readiness…" />
  if (readiness.error && !readiness.data) return <ErrorNote error={readiness.error} />
  if (!readiness.data) return <Empty>Readiness unavailable.</Empty>

  return (
    <div className="space-y-4">
      <Card
        title="How this number is produced"
        subtitle={readiness.data.readiness_method}
        tone="caution"
      >
        <p className="text-xs leading-relaxed text-ink-muted">{readiness.data.readiness_note}</p>
        <p className="mt-2 text-[11px] text-ink-faint">
          Colour is always paired with a word here: a reviewer in greyscale, or a reader using a
          screen reader, loses no information.
        </p>
      </Card>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        {(['ALL', 'AVAILABLE', 'LIMITED', 'UNAVAILABLE'] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setFilter(value)}
            aria-pressed={filter === value}
            className={`rounded border px-2.5 py-1 text-[11px] font-semibold ${
              filter === value
                ? 'border-accent bg-accent/15 text-accent'
                : 'border-edge-strong text-ink-muted hover:text-ink'
            }`}
          >
            {value}
          </button>
        ))}

        <label className="ml-auto flex items-center gap-2 text-[11px] text-ink-faint">
          sort by
          <select
            value={sort}
            onChange={(event) => setSort(event.target.value as typeof sort)}
            className="rounded border border-edge-strong bg-surface-sunken px-2 py-1 text-[11px]"
          >
            <option value="readiness">readiness (lowest first)</option>
            <option value="maintenance">maintenance hours</option>
            <option value="id">asset id</option>
          </select>
        </label>
      </div>

      {rows.length === 0 ? (
        <Empty>No assets match this filter.</Empty>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {rows.map((asset) => (
            <ReadinessCard key={asset.id} asset={asset} />
          ))}
        </div>
      )}

      <CrewDuty tick={tick} />
    </div>
  )
}

function ReadinessCard({ asset }: { asset: AssetRow }) {
  const band: Band =
    asset.readiness_probability < 0.7 ? 'low' : asset.readiness_probability < 0.9 ? 'mid' : 'high'
  const pct = Math.round(asset.readiness_probability * 100)
  const lowPct = Math.round(asset.readiness_low * 100)
  const highPct = Math.round(asset.readiness_high * 100)

  return (
    <article
      data-testid={`asset-${asset.id}`}
      className="rounded-lg border border-edge bg-surface-raised p-3 text-xs"
    >
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="tabular text-sm font-bold">{asset.id}</h3>
          <p className="text-[11px] text-ink-faint">{asset.label}</p>
        </div>
        <div className="flex flex-wrap justify-end gap-1">
          <Badge tone={BAND_TONE[asset.readiness_band] ?? 'neutral'}>
            readiness {asset.readiness_band}
          </Badge>
          <Badge tone={STATUS_TONE[asset.status] ?? 'neutral'}>{asset.status}</Badge>
        </div>
      </header>

      <div className="mt-3">
        <div className="flex items-baseline justify-between">
          <span className="tabular text-lg font-bold">{asset.readiness_probability.toFixed(2)}</span>
          <span className="tabular text-[11px] text-ink-faint">
            interval {lowPct}–{highPct}
          </span>
        </div>
        <div className="relative mt-1 h-2 overflow-hidden rounded bg-surface-sunken">
          <div
            className={`h-full ${
              band === 'low' ? 'bg-rose-500/70' : band === 'mid' ? 'bg-caution/70' : 'bg-emerald-500/60'
            }`}
            style={{ width: `${pct}%` }}
          />
          <div
            className="absolute inset-y-0 border-x border-ink/40"
            style={{ left: `${lowPct}%`, width: `${Math.max(highPct - lowPct, 1)}%` }}
            aria-hidden="true"
          />
        </div>
        <p className="mt-1 text-[10px] text-ink-faint">
          Point estimate with its synthetic uncertainty band. Not a measured probability.
        </p>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <div className="flex justify-between">
          <dt className="text-ink-faint">class</dt>
          <dd className="tabular">{asset.class}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-ink-faint">home hub</dt>
          <dd className="tabular">{asset.home_hub_id}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-ink-faint">maintenance hours</dt>
          <dd className="tabular">{asset.maintenance_hours_since.toFixed(1)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-ink-faint">recent faults</dt>
          <dd className="tabular">{asset.recent_fault_count}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-ink-faint">available from</dt>
          <dd className="tabular">minute {asset.available_from_minute}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-ink-faint">endurance</dt>
          <dd className="tabular">{asset.endurance_minutes} min</dd>
        </div>
      </dl>

      <p className="mt-2 border-t border-edge pt-2 text-[11px] text-ink-muted">
        {asset.assigned_mission_id
          ? `Assigned to ${asset.assigned_mission_id} in the active plan.`
          : 'Unassigned in the active plan.'}
      </p>
    </article>
  )
}

/**
 * Crew duty is shown here because crew is the other half of the no-overlap story: a plan can
 * be asset-feasible and still be impossible if the qualified crew is already on a sortie.
 */
function CrewDuty({ tick }: { tick: RefreshTick }) {
  const crew = useAsync(() => api.crew(), [tick])

  if (crew.loading && !crew.data) return <Loading label="Loading crew duty…" />
  if (crew.error && !crew.data) return <ErrorNote error={crew.error} />
  if (!crew.data) return null

  const rows = crew.data.crew.map((entry) => {
    const row = entry as Record<string, unknown>
    const used = Number(row['duty_minutes_used'] ?? 0)
    const limit = Number(row['duty_limit_minutes'] ?? 1)
    const remaining = Number(row['duty_remaining_minutes'] ?? 0)
    const rest = Number(row['min_rest_minutes'] ?? 0)
    const assigned = (row['assigned_mission_ids'] as string[] | undefined) ?? []
    return {
      id: String(row['id']),
      label: String(row['label'] ?? ''),
      hub: String(row['home_hub_id'] ?? ''),
      roles: (row['roles'] as string[] | undefined) ?? [],
      classes: (row['qualified_classes'] as string[] | undefined) ?? [],
      used,
      limit,
      remaining,
      rest,
      assigned,
    }
  })

  return (
    <Card
      title="Crew duty"
      subtitle="Duty minutes are a hard constraint: a crew member cannot be assigned to two overlapping sorties, and must meet minimum rest between them."
    >
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
            <tr>
              <th className="py-1 pr-2">Crew</th>
              <th className="pr-2">Hub</th>
              <th className="pr-2">Roles</th>
              <th className="pr-2">Qualified for</th>
              <th className="pr-2">Duty used</th>
              <th className="pr-2">Remaining</th>
              <th>Assigned to</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const used = Math.min(row.used, row.limit)
              return (
                <tr key={row.id} className="border-t border-edge align-top">
                  <td className="tabular py-1 pr-2 font-semibold">{row.id}</td>
                  <td className="tabular pr-2">{row.hub}</td>
                  <td className="pr-2 text-ink-faint">{row.roles.join(', ')}</td>
                  <td className="pr-2 text-ink-faint">{row.classes.join(', ')}</td>
                  <td className="pr-2">
                    <span className="flex items-center gap-1.5">
                      <span className="h-1.5 w-16 overflow-hidden rounded bg-surface-sunken">
                        <span
                          className={`block h-full ${
                            used / row.limit > 0.8 ? 'bg-caution' : 'bg-ink-muted'
                          }`}
                          style={{ width: `${Math.round((used / row.limit) * 100)}%` }}
                        />
                      </span>
                      <span className="tabular text-ink-faint">
                        {row.used}/{row.limit}
                      </span>
                    </span>
                  </td>
                  <td className="tabular pr-2">
                    {row.remaining} min
                    {row.rest > 0 && (
                      <span className="text-ink-faint"> (rest {row.rest})</span>
                    )}
                  </td>
                  <td className="text-ink-muted">
                    {row.assigned.length > 0 ? row.assigned.join(', ') : 'unassigned'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Card>
  )
}