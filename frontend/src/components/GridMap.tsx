/**
 * Neutral schematic map.
 *
 * This is a grid plot, not a real map: there are no coastlines, place names, or terrain.
 * Coordinates are synthetic units inside the scenario's own bounding box. Rendering is
 * plain SVG, so there is no mapping library and no tiles to fetch — the whole view works
 * with the network cable unplugged.
 */
import { useMemo } from 'react'

import type { MapPayload } from '../types/api'

const HUB_R = 7
const ASSET_R = 3.2
const SPREAD_R = 11

function assetTone(status: string, airborne: boolean): { fill: string; label: string } {
  if (airborne) return { fill: '#38bdf8', label: 'Airborne — assignment frozen' }
  if (status === 'UNAVAILABLE') return { fill: '#fbbf24', label: 'Asset unavailable' }
  if (status === 'LIMITED') return { fill: '#a78bfa', label: 'Asset limited' }
  return { fill: '#94a3b8', label: 'Asset available' }
}

function zoneTone(severity: number, blocking: boolean): string {
  if (blocking) return '#f43f5e'
  if (severity >= 0.7) return '#fbbf24'
  return '#a78bfa'
}

export function GridMap({
  data,
  selectedMissionId,
  onSelectMission,
}: {
  data: MapPayload
  selectedMissionId?: string | null
  onSelectMission?: (missionId: string) => void
}) {
  const w = data.width_units
  const h = data.height_units
  const now = data.now_minute

  // Cells arrive as [x, y] pairs. Reducing them to one bounding rectangle per zone keeps
  // the DOM small; the zones in the seeded scenario are rectangles by construction, and a
  // non-rectangular zone still renders a truthful (if generous) extent.
  const zoneRects = useMemo(
    () =>
      data.hazard_zones.map((zone) => {
        const xs = zone.cells.map((cell) => cell[0])
        const ys = zone.cells.map((cell) => cell[1])
        const x = Math.min(...xs)
        const y = Math.min(...ys)
        return {
          zone,
          x,
          y,
          width: Math.max(...xs) - x + 1,
          height: Math.max(...ys) - y + 1,
          active: now >= zone.active_start && now < zone.active_end,
        }
      }),
    [data.hazard_zones, now],
  )

  const gridLines = useMemo(() => {
    const vertical: number[] = []
    const horizontal: number[] = []
    const step = Math.max(1, Math.round(w / 12))
    for (let x = 0; x <= w; x += step) vertical.push(x)
    for (let y = 0; y <= h; y += step) horizontal.push(y)
    return { vertical, horizontal }
  }, [w, h])

  return (
    <div>
      <svg
        viewBox={`-6 -8 ${w + 12} ${h + 18}`}
        className="w-full rounded border border-edge bg-surface-sunken"
        role="img"
        aria-label="Synthetic schematic grid: fictional hubs, assets, mission areas, hazard zones and weather restriction windows. No real geography."
      >
        <g stroke="#1e293b" strokeWidth={0.04}>
          {gridLines.vertical.map((x) => (
            <line key={`v${x}`} x1={x} y1={0} x2={x} y2={h} />
          ))}
          {gridLines.horizontal.map((y) => (
            <line key={`h${y}`} x1={0} y1={y} x2={w} y2={y} />
          ))}
        </g>

        {/* Weather restriction windows: a dashed ring around the hub they apply to. */}
        {data.weather_windows.map((window) => {
          const hub = data.hubs.find((candidate) => candidate.id === window.hub_id)
          if (!hub) return null
          const active = now >= window.start && now < window.end
          const colour = window.blocking ? '#fbbf24' : '#64748b'
          return (
            <g key={window.id}>
              <circle
                cx={hub.x}
                cy={hub.y}
                r={SPREAD_R + 4}
                fill="none"
                stroke={colour}
                strokeWidth={0.25}
                strokeDasharray="1 1"
                opacity={active ? 0.9 : 0.3}
              >
                <title>
                  {window.name} — {window.restriction_type}, severity {window.severity.toFixed(2)},{' '}
                  {window.blocking ? 'BLOCKING' : 'advisory'} · minutes {window.start}–{window.end}
                  {active ? ' (active now)' : ''}
                </title>
              </circle>
              <text
                x={hub.x}
                y={hub.y - SPREAD_R - 6}
                textAnchor="middle"
                fontSize={2.5}
                fill={colour}
                opacity={active ? 0.95 : 0.4}
              >
                {window.name}
              </text>
            </g>
          )
        })}

        {/* Hazard zones. Dashed and faded when not currently active. */}
        {zoneRects.map(({ zone, x, y, width, height, active }) => {
          const colour = zoneTone(zone.severity, zone.blocking)
          return (
            <rect
              key={zone.id}
              x={x}
              y={y}
              width={width}
              height={height}
              fill={colour}
              fillOpacity={active ? 0.26 : 0.08}
              stroke={colour}
              strokeWidth={zone.blocking ? 0.3 : 0.15}
              strokeDasharray={zone.blocking ? '1 0.6' : '0.6 0.6'}
              opacity={active ? 1 : 0.4}
            >
              <title>
                {zone.name} — severity {zone.severity.toFixed(2)} ({zone.band}) ·{' '}
                {zone.blocking ? 'BLOCKING hard restriction' : 'advisory only'} · minutes{' '}
                {zone.active_start}–{zone.active_end}
                {active ? ' · active now' : ''}
              </title>
            </rect>
          )
        })}

        {/* Routes from each mission's origin hub to its objective. */}
        {data.assignments.map((assignment) => {
          const mission = data.missions.find((candidate) => candidate.id === assignment.mission_id)
          const hub = data.hubs.find((candidate) => candidate.id === mission?.origin_hub_id)
          if (!mission || !hub) return null
          return (
            <line
              key={`route-${assignment.mission_id}`}
              x1={hub.x}
              y1={hub.y}
              x2={mission.objective_x}
              y2={mission.objective_y}
              stroke={assignment.is_frozen ? '#38bdf8' : '#475569'}
              strokeWidth={assignment.is_frozen ? 0.3 : 0.18}
              strokeDasharray={assignment.is_frozen ? undefined : '0.8 0.6'}
              opacity={0.85}
            />
          )
        })}

        {/* Mission objectives: a diamond, filled when the active plan covers it. */}
        {data.missions.map((mission) => {
          const selected = mission.id === selectedMissionId
          const assigned = mission.assigned_asset_id !== null
          return (
            <g
              key={mission.id}
              onClick={() => onSelectMission?.(mission.id)}
              style={{ cursor: onSelectMission ? 'pointer' : 'default' }}
            >
              <rect
                x={mission.objective_x - 1.5}
                y={mission.objective_y - 1.5}
                width={3}
                height={3}
                fill={assigned ? '#38bdf8' : '#0b1120'}
                fillOpacity={assigned ? 0.35 : 0.85}
                stroke={selected ? '#fbbf24' : assigned ? '#38bdf8' : '#64748b'}
                strokeWidth={selected ? 0.45 : 0.22}
                transform={`rotate(45 ${mission.objective_x} ${mission.objective_y})`}
              >
                <title>
                  {mission.id} — {mission.title} (P{mission.priority}) ·{' '}
                  {assigned ? `assigned to ${mission.assigned_asset_id}` : 'unassigned in active plan'}
                </title>
              </rect>
            </g>
          )
        })}

        {/* Assets, fanned out around their home hub so markers stay separable. */}
        {data.hubs.map((hub) => {
          const based = data.assets.filter((asset) => asset.home_hub_id === hub.id)
          return (
            <g key={`assets-${hub.id}`}>
              {based.map((asset, index) => {
                const tone = assetTone(asset.status, asset.is_airborne)
                const angle = (index / Math.max(based.length, 1)) * Math.PI * 2
                return (
                  <circle
                    key={asset.id}
                    cx={hub.x + Math.cos(angle) * SPREAD_R}
                    cy={hub.y + Math.sin(angle) * SPREAD_R}
                    r={ASSET_R}
                    fill={tone.fill}
                    fillOpacity={0.92}
                    stroke="#0b1120"
                    strokeWidth={0.15}
                  >
                    <title>
                      {asset.id} — {asset.label} ({asset.class}) · {tone.label} · readiness{' '}
                      {asset.readiness_probability.toFixed(2)} ({asset.readiness_band})
                      {asset.assigned_mission_id ? ` · flying ${asset.assigned_mission_id}` : ''}
                    </title>
                  </circle>
                )
              })}
            </g>
          )
        })}

        {/* Hubs last so they sit above the asset ring. */}
        {data.hubs.map((hub) => (
          <g key={hub.id}>
            <circle
              cx={hub.x}
              cy={hub.y}
              r={HUB_R}
              fill="#0b1120"
              stroke={hub.status === 'OPEN' ? '#38bdf8' : '#fbbf24'}
              strokeWidth={0.4}
            />
            <text
              x={hub.x}
              y={hub.y + 1.1}
              textAnchor="middle"
              fontSize={3.2}
              fontWeight="700"
              fill="#e2e8f0"
            >
              {hub.id.replace(/^HUB-/, '')}
            </text>
            <text x={hub.x} y={hub.y + HUB_R + 3.6} textAnchor="middle" fontSize={2.3} fill="#94a3b8">
              {hub.name}
            </text>
            <title>
              {hub.id} — {hub.name} (fictional) · runway capacity {hub.runway_capacity_per_slot} per{' '}
              {hub.slot_minutes}-minute slot · status {hub.status}
            </title>
          </g>
        ))}
      </svg>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] text-ink-muted">
        {data.legend.map((entry) => (
          <span key={entry.key} className="inline-flex items-center gap-1.5">
            <LegendSwatch kind={entry.key} />
            {entry.label}
          </span>
        ))}
        <span className="tabular ml-auto text-ink-faint">
          grid {w}×{h} units · cell {data.grid_cell} · clock minute {now}
        </span>
      </div>
    </div>
  )
}

function LegendSwatch({ kind }: { kind: string }) {
  switch (kind) {
    case 'hub':
      return (
        <span className="inline-block h-2.5 w-2.5 rounded-full border-2 border-accent bg-surface-sunken" />
      )
    case 'asset_available':
      return <span className="inline-block h-2.5 w-2.5 rounded-full bg-ink-muted" />
    case 'asset_limited':
      return <span className="inline-block h-2.5 w-2.5 rounded-full bg-advisory" />
    case 'asset_unavailable':
      return <span className="inline-block h-2.5 w-2.5 rounded-full bg-caution" />
    case 'airborne':
      return <span className="inline-block h-2.5 w-2.5 rounded-full bg-accent" />
    case 'zone_restricted':
      return <span className="inline-block h-2.5 w-3 border border-rose-400 bg-rose-500/30" />
    case 'zone_elevated':
      return <span className="inline-block h-2.5 w-3 border border-caution bg-caution/25" />
    case 'zone_advisory':
      return <span className="inline-block h-2.5 w-3 border border-advisory bg-advisory/20" />
    case 'weather':
      return (
        <span className="inline-block h-2.5 w-3 rounded-full border border-dashed border-caution" />
      )
    default:
      return <span className="inline-block h-2.5 w-3 rounded-full border border-dashed border-ink-faint" />
  }
}

/** Headline counts for the command deck, derived from the same payload the map uses. */
export function MapSummary({ data }: { data: MapPayload }) {
  const assigned = data.missions.filter((mission) => mission.assigned_asset_id !== null).length
  const airborne = data.assets.filter((asset) => asset.is_airborne).length
  const blockingActive = data.hazard_zones.filter(
    (zone) => zone.blocking && data.now_minute >= zone.active_start && data.now_minute < zone.active_end,
  ).length
  const weatherActive = data.weather_windows.filter(
    (window) => data.now_minute >= window.start && data.now_minute < window.end,
  ).length

  return (
    <div className="flex flex-wrap gap-2 text-[11px]">
      <Pill>{`${assigned}/${data.missions.length} missions assigned`}</Pill>
      {airborne > 0 && <Pill tone="accent">{`${airborne} airborne (frozen)`}</Pill>}
      {blockingActive > 0 && (
        <Pill tone="bad">{`${blockingActive} blocking zone(s) active now`}</Pill>
      )}
      <Pill>{`${weatherActive}/${data.weather_windows.length} weather window(s) active`}</Pill>
    </div>
  )
}

function Pill({ children, tone = 'neutral' }: { children: string; tone?: 'neutral' | 'accent' | 'bad' }) {
  const tones: Record<string, string> = {
    neutral: 'border-edge-strong bg-surface-sunken text-ink-muted',
    accent: 'border-accent/50 bg-accent/10 text-accent',
    bad: 'border-rose-500/50 bg-rose-500/10 text-rose-300',
  }
  return (
    <span className={`rounded border px-2 py-0.5 ${tones[tone]}`}>{children}</span>
  )
}