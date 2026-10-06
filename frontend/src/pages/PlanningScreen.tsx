/**
 * Screen B — Planning.
 *
 * Generates exactly three trade-off options and shows them side by side. The spec requires
 * the three to be *visibly* different, so the cards are compared on the same axes
 * (weighted coverage, mean risk, changes, assigned missions) rather than described in prose.
 *
 * The spec also asks for "exactly three distinct options". That is not achievable on every
 * state — after a disruption the planner re-opens only the affected missions, and when that
 * leaves one decision all three objectives genuinely agree. When that happens the screen
 * says so and marks the identical cards rather than dressing one up as a real trade-off.
 *
 * The safety floor is surfaced explicitly: SAFETY FIRST and STABILITY FIRST may not drop
 * below 85% of COVERAGE FIRST's weighted coverage, and each card shows that floor.
 */
import { useMemo, useState } from 'react'

import { api, ApiError } from '../api/client'
import { OptionDistinctnessNote } from '../components/OptionDistinctness'
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Metric,
  PriorityPill,
  RiskBar,
} from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import { useRole } from '../hooks/useRole'
import type { GeneratePlansResponse, Mission, Plan } from '../types/api'
import type { RefreshTick } from '../App'

const VARIANT_ORDER = ['COVERAGE FIRST', 'SAFETY FIRST', 'STABILITY FIRST']
const FLOOR_RATIO = 0.85

export function PlanningScreen({
  tick,
  onChanged,
}: {
  tick: RefreshTick
  onChanged: () => void
}) {
  const { capabilities } = useRole()
  const [result, setResult] = useState<GeneratePlansResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; forbidden: boolean } | null>(null)

  const stored = useAsync(() => api.plans(), [tick])
  const missions = useAsync(() => api.missions(), [tick])

  // Priority and title live on the mission, not the assignment, so they come from
  // /missions. If that call has not landed the table degrades to plain mission ids rather
  // than inventing a priority.
  const missionIndex = useMemo(() => {
    const index = new Map<string, Mission>()
    for (const mission of missions.data?.missions ?? []) index.set(mission.id, mission)
    return index
  }, [missions.data])

  const generate = async (revise: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const response = await api.generatePlans(revise)
      setResult(response)
      setSelected(response.plans[0]?.id ?? null)
      onChanged()
    } catch (caught) {
      setError({
        message: caught instanceof Error ? caught.message : 'Generation failed.',
        forbidden: caught instanceof ApiError && caught.isForbidden,
      })
    } finally {
      setBusy(false)
    }
  }

  const plans = result?.plans ?? []
  const reference = plans.find((plan) => plan.variant === 'COVERAGE FIRST') ?? plans[0] ?? null
  const floor = reference ? Math.ceil(reference.metrics.weighted_coverage * FLOOR_RATIO) : 0
  const activePlan = stored.data?.plans.find((plan) => plan.status === 'ACTIVE') ?? null
  const detail = plans.find((plan) => plan.id === selected) ?? reference

  return (
    <div className="space-y-4">
      <Card
        title="Generate the three-option frontier"
        subtitle="CP-SAT solve with a deterministic seed, an 8-second budget, and a labelled greedy fallback."
        actions={
          <>
            <Button
              onClick={() => void generate(false)}
              disabled={busy || !capabilities?.can_generate_plans}
              testId="generate-plans"
              title={
                capabilities?.can_generate_plans
                  ? 'Solve from scratch, ignoring any active plan'
                  : 'Your role cannot generate plans'
              }
            >
              {busy ? 'Solving…' : 'Generate initial plans'}
            </Button>
            <Button
              onClick={() => void generate(true)}
              disabled={busy || !capabilities?.can_generate_plans || !activePlan}
              variant="primary"
              title={
                activePlan
                  ? 'Re-plan with the active plan as the parent, keeping started sorties frozen'
                  : 'No active plan to revise'
              }
            >
              Revise active plan
            </Button>
          </>
        }
      >
        {error && <ErrorNote error={error.message} forbidden={error.forbidden} />}

        <p className="text-xs leading-relaxed text-ink-muted">
          The planner produces <strong>options</strong>, not orders. Nothing here becomes the
          active plan until a Commander approves a proposal on the disruption centre. If the
          solver exceeds its budget the engine falls back to a deterministic greedy pass and
          labels the result <code className="text-caution">FALLBACK</code>, so a reviewer can
          tell the difference at a glance.
        </p>

        {result && (
          <>
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded border border-edge bg-surface-sunken px-3 py-2 text-[11px]">
              <span className="tabular">
                total solve time <strong>{result.solve_time_ms_total} ms</strong>
              </span>
              <span className="tabular">
                distinct variants <strong>{result.distinct_variants}</strong>
              </span>
              <span className="tabular">
                coverage reference (weighted) <strong>{result.coverage_reference ?? '—'}</strong>
              </span>
              <span className="text-ink-faint">{result.activation_note}</span>
            </div>

            <OptionDistinctnessNote distinctness={result} optionsShown={plans.length} />
          </>
        )}
      </Card>

      {plans.length > 0 && (
        <div className="grid gap-3 lg:grid-cols-3" data-testid="plan-cards">
          {VARIANT_ORDER.map((variant) => {
            const plan = plans.find((candidate) => candidate.variant === variant)
            if (!plan) return null
            return (
              <PlanCard
                key={plan.id}
                plan={plan}
                floor={floor}
                isSelected={selected === plan.id}
                identicalTo={
                  result?.duplicate_options
                    .find((group) => group.includes(plan.variant))
                    ?.filter((other) => other !== plan.variant) ?? []
                }
                onSelect={() => setSelected(plan.id)}
              />
            )
          })}
        </div>
      )}

      {detail && (
        <Card
          title="Assignment explanations"
          subtitle="Rendered from stored reason codes by fixed templates. No language model is involved."
        >
          <PlanDetail plan={detail} missionIndex={missionIndex} />
        </Card>
      )}

      {plans.length === 0 && !busy && (
        <Card title="Stored plan versions" subtitle="Every version the scenario has produced.">
          {stored.loading ? (
            <Loading />
          ) : stored.error ? (
            <ErrorNote error={stored.error} />
          ) : (stored.data?.plans.length ?? 0) === 0 ? (
            <Empty>No plans stored yet. Generate the initial set above.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
                  <tr>
                    <th className="py-1">Plan</th>
                    <th>Variant</th>
                    <th>Status</th>
                    <th>Covered</th>
                    <th>Mean risk</th>
                    <th className="text-right">Solve</th>
                  </tr>
                </thead>
                <tbody>
                  {stored.data!.plans.map((plan) => (
                    <tr key={plan.id} className="border-t border-edge">
                      <td className="tabular py-1 font-semibold">{plan.id}</td>
                      <td>{plan.variant}</td>
                      <td>{plan.status_label ?? plan.status}</td>
                      <td className="tabular">
                        {plan.metrics.missions_covered}/{plan.metrics.missions_total}
                      </td>
                      <td className="tabular">{plan.metrics.mean_risk.toFixed(3)}</td>
                      <td className="tabular text-right">{plan.solve_time_ms} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  )
}

function PlanCard({
  plan,
  floor,
  isSelected,
  identicalTo,
  onSelect,
}: {
  plan: Plan
  floor: number
  isSelected: boolean
  identicalTo: string[]
  onSelect: () => void
}) {
  const { metrics } = plan
  return (
    <article
      data-testid={`plan-card-${plan.variant.slice(0, plan.variant.indexOf(' ')).toLowerCase()}`}
      className={`cursor-pointer rounded-lg border bg-surface-raised p-3 transition-colors ${
        isSelected ? 'border-accent' : 'border-edge hover:border-edge-strong'
      }`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
      role="button"
      tabIndex={0}
      aria-pressed={isSelected}
    >
      <header className="mb-2 flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-bold tracking-wide">{plan.variant}</h3>
          <p className="tabular text-[11px] text-ink-faint">{plan.id}</p>
        </div>
        <div className="flex flex-wrap justify-end gap-1">
          {plan.is_fallback && <Badge tone="caution">FALLBACK</Badge>}
          {identicalTo.length > 0 && (
            <Badge tone="caution" title="The solver produced the same assignments as the other option(s) named here">
              = {identicalTo.join(' = ')}
            </Badge>
          )}
          <Badge tone={plan.auditor_valid ? 'good' : 'bad'}>
            {plan.auditor_valid ? 'AUDITOR-VALID' : 'AUDITOR-INVALID'}
          </Badge>
        </div>
      </header>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
        <Metric
          label="Weighted coverage"
          value={metrics.weighted_coverage.toFixed(0)}
          detail={
            plan.variant === 'COVERAGE FIRST'
              ? 'reference value for the safety floor'
              : `floor ${floor} (85% of reference)`
          }
        />
        <Metric
          label="Missions"
          value={`${metrics.missions_covered}/${metrics.missions_total}`}
          detail={`P1 ${metrics.p1_covered}/${metrics.p1_total}`}
        />
        <Metric
          label="Mean risk"
          value={metrics.mean_risk.toFixed(3)}
          detail={
            metrics.risk_savings_vs_coverage_first !== 0
              ? `${
                  metrics.risk_savings_vs_coverage_first > 0 ? 'saves' : 'adds'
                } ${Math.abs(metrics.risk_savings_vs_coverage_first).toFixed(3)} vs coverage-first`
              : 'baseline for the other options'
          }
        />
        <Metric
          label="Changes vs parent"
          value={metrics.changed_assignments}
          detail={`${metrics.frozen_assignments} assignment(s) frozen`}
        />
      </dl>

      {plan.variant !== 'COVERAGE FIRST' && (
        <p
          className={`tabular mt-2 text-[11px] ${
            metrics.weighted_coverage >= floor ? 'text-emerald-300' : 'text-rose-300'
          }`}
        >
          {metrics.weighted_coverage >= floor
            ? `Above the safety floor (${metrics.weighted_coverage.toFixed(0)} ≥ ${floor}).`
            : `BELOW the safety floor (${metrics.weighted_coverage.toFixed(0)} < ${floor}) — this should be unreachable.`}
        </p>
      )}

      <div className="mt-2">
        <RiskBar value={metrics.mean_risk} label={`mean risk for ${plan.variant}`} />
      </div>

      <p className="mt-2 text-[11px] text-ink-faint">
        {metrics.missions_covered === metrics.missions_total
          ? 'All assignable missions covered.'
          : `Unassigned: ${plan.unassigned_mission_ids.join(', ') || 'none'}`}
      </p>
    </article>
  )
}

function PlanDetail({
  plan,
  missionIndex,
}: {
  plan: Plan
  missionIndex: Map<string, Mission>
}) {
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <Badge tone="accent">{plan.variant}</Badge>
        <span className="tabular font-semibold">{plan.id}</span>
        <span className="text-ink-faint">
          solver status {plan.solver_status} in {plan.solve_time_ms} ms
        </span>
        {plan.solver_status.toUpperCase().includes('FALLBACK') && (
          <Badge tone="caution" title="The solver exceeded its budget; a greedy pass produced this plan">
            Greedy fallback used
          </Badge>
        )}
      </div>

      {plan.key_reasons && plan.key_reasons.length > 0 && (
        <ul className="mb-3 space-y-1 text-xs text-ink-muted">
          {plan.key_reasons.map((reason) => (
            <li key={reason} className="border-l-2 border-accent/50 pl-2">
              {reason}
            </li>
          ))}
        </ul>
      )}

      {plan.notes.length > 0 && (
        <ul className="mb-3 space-y-1 text-[11px] text-ink-faint">
          {plan.notes.map((note) => (
            <li key={note}>— {note}</li>
          ))}
        </ul>
      )}

      {!plan.auditor_valid && (
        <div className="mb-3 rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          <strong className="font-semibold">
            The independent auditor rejected this plan.
          </strong>
          <ul className="mt-1 list-disc pl-4">
            {plan.auditor_findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
        </div>
      )}

      {plan.assignments.length === 0 ? (
        <Empty>
          This option assigns nothing. It is still shown because the frontier must always
          return three options, and the difference between them is the point of the screen.
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
              <tr>
                <th className="py-1 pr-2">Mission</th>
                <th className="pr-2">Asset</th>
                <th className="pr-2">Crew</th>
                <th className="pr-2">Takeoff</th>
                <th className="pr-2">Return</th>
                <th className="pr-2">Risk</th>
                <th>Why this assignment</th>
              </tr>
            </thead>
            <tbody>
              {plan.assignments.map((assignment) => {
                const mission = missionIndex.get(assignment.mission_id)
                return (
                  <tr key={assignment.id} className="border-t border-edge align-top">
                    <td className="tabular py-1 pr-2 font-semibold">
                      {mission && (
                        <span className="mr-1 inline-flex">
                          <PriorityPill priority={mission.priority} />
                        </span>
                      )}
                      {assignment.mission_id}
                    </td>
                    <td className="tabular pr-2">
                      {assignment.asset_id}
                      {assignment.is_frozen && (
                        <Badge
                          tone="accent"
                          title="Takeoff has already passed; re-planning cannot change this"
                        >
                          FROZEN
                        </Badge>
                      )}
                    </td>
                    <td className="tabular pr-2 text-ink-faint">{assignment.crew_ids.join(', ')}</td>
                    <td className="tabular pr-2">{assignment.takeoff_minute}</td>
                    <td className="tabular pr-2">{assignment.return_minute}</td>
                    <td className="tabular pr-2">{assignment.risk.toFixed(3)}</td>
                    <td className="max-w-md text-ink-muted">
                      {assignment.explanation || '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {plan.unassigned_mission_ids.length > 0 && (
        <p className="mt-2 text-xs text-caution">
          Unassigned in this option: {plan.unassigned_mission_ids.join(', ')}. Open any of them
          on the command deck to see which hard constraints blocked the remaining options.
        </p>
      )}
    </div>
  )
}