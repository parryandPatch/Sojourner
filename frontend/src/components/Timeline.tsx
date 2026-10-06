/**
 * Assignment timeline (a lightweight Gantt).
 *
 * Bars are drawn against scenario minutes, not wall-clock time, so the shape of the plan is
 * readable directly. Frozen (already-taken-off) assignments are drawn solid and the rest
 * dashed, which is the distinction the disruption screen's stability story depends on.
 */
import { useMemo } from 'react'

import type { TimelineRow } from '../types/api'
import { PriorityPill } from './ui'

export function Timeline({
  rows,
  nowMinute,
  horizonMinutes,
}: {
  rows: TimelineRow[]
  nowMinute: number
  horizonMinutes: number
}) {
  const span = Math.max(horizonMinutes, 1)

  const ticks = useMemo(() => {
    const step = span > 480 ? 120 : 60
    const out: number[] = []
    for (let minute = 0; minute <= span; minute += step) out.push(minute)
    return out
  }, [span])

  if (rows.length === 0) {
    return (
      <p className="rounded border border-dashed border-edge px-3 py-6 text-center text-xs text-ink-faint">
        No active plan, so there is nothing on the timeline. Generate a plan set on the planning
        screen, then have a Commander approve one to activate it.
      </p>
    )
  }

  return (
    <div>
      <div className="relative mb-1 ml-[13.5rem] h-4">
        {ticks.map((minute) => (
          <span
            key={minute}
            className="tabular absolute -translate-x-1/2 text-[10px] text-ink-faint"
            style={{ left: `${(minute / span) * 100}%` }}
          >
            {minute}
          </span>
        ))}
      </div>

      <ul className="space-y-1">
        {rows.map((row) => {
          const left = (row.start_minute / span) * 100
          const width = Math.max(((row.end_minute - row.start_minute) / span) * 100, 0.8)
          return (
            <li key={row.mission_id} className="flex items-center gap-2">
              <div className="flex w-52 shrink-0 items-center gap-1.5">
                <PriorityPill priority={row.priority} />
                <span className="tabular truncate text-[11px] text-ink" title={row.mission_title}>
                  {row.mission_id}
                </span>
              </div>

              <div className="relative h-5 flex-1 overflow-hidden rounded bg-surface-sunken">
                {/* now marker */}
                <div
                  className="absolute top-0 h-full w-px bg-caution"
                  style={{ left: `${(nowMinute / span) * 100}%` }}
                  aria-hidden="true"
                />
                <div
                  className={`absolute top-1 h-3 rounded-sm ${
                    row.is_frozen
                      ? 'bg-accent/70'
                      : 'bg-edge-strong bg-[repeating-linear-gradient(45deg,#475569,#475569_4px,#334155_4px,#334155_8px)]'
                  }`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={`${row.mission_id} · ${row.asset_id} · minutes ${row.start_minute}–${row.end_minute} · risk ${row.risk.toFixed(3)} · ${row.phase}`}
                  data-testid={`timeline-${row.mission_id}`}
                />
              </div>

              <div className="flex w-24 shrink-0 items-center justify-end gap-1.5">
                <span className="tabular text-[10px] text-ink-faint" title={`asset ${row.asset_id}`}>
                  {row.asset_id.replace(/^AST-/, '')}
                </span>
                <span
                  className={`tabular text-[10px] ${
                    row.phase === 'AIRBORNE'
                      ? 'text-accent'
                      : row.phase === 'DEPARTED'
                        ? 'text-ink-faint'
                        : 'text-ink-muted'
                  }`}
                  title={`phase ${row.phase}${row.is_frozen ? ' · frozen, cannot be changed by re-planning' : ''}`}
                >
                  {row.phase === 'AIRBORNE'
                    ? 'AIRBORNE'
                    : row.phase === 'DEPARTED'
                      ? 'DEPARTED'
                      : row.start_minute <= nowMinute
                        ? 'TAKEOFF'
                        : 'PLANNED'}
                </span>
              </div>
            </li>
          )
        })}
      </ul>

      <p className="mt-2 text-[11px] text-ink-faint">
        Solid bars are frozen: their takeoff has already passed, so re-planning cannot change
        them. The amber line is the current operational clock position.
      </p>
    </div>
  )
}