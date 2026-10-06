/**
 * Screen C — Disruption centre.
 *
 * This is where a synthetic disruption becomes a decision a human has to make. The flow is:
 *
 *   inject scripted event → engine re-plans only the affected missions → three proposals
 *   appear as PENDING → a Commander approves or rejects one → only then does a plan activate.
 *
 * The Approve button is deliberately unguarded in appearance but always guarded by the
 * server: a non-Commander gets HTTP 403 and the UI shows that as the expected result rather
 * than hiding the control, so a reviewer can see the boundary.
 */
import { useState } from 'react'

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
} from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import { useRole } from '../hooks/useRole'
import type { InjectEventResponse, Proposal } from '../types/api'
import type { RefreshTick } from '../App'

type ActionOutcome =
  | { kind: 'idle' }
  | { kind: 'ok'; message: string }
  | { kind: 'error'; message: string; forbidden: boolean; conflict: boolean }

export function DisruptionCentre({
  tick,
  onChanged,
}: {
  tick: RefreshTick
  onChanged: () => void
}) {
  const { capabilities, role } = useRole()
  const scripted = useAsync(() => api.scriptedEvents(), [])
  const proposals = useAsync(() => api.proposals(), [tick])
  const [injecting, setInjecting] = useState<string | null>(null)
  const [injection, setInjection] = useState<InjectEventResponse | null>(null)
  const [outcome, setOutcome] = useState<ActionOutcome>({ kind: 'idle' })

  const inject = async (key: string) => {
    setInjecting(key)
    setOutcome({ kind: 'idle' })
    try {
      const response = await api.injectEvent(key)
      setInjection(response)
      onChanged()
    } catch (caught) {
      setInjection(null)
      setOutcome(toOutcome(caught))
    } finally {
      setInjecting(null)
    }
  }

  const pending = (proposals.data?.proposals ?? []).filter((proposal) => proposal.status === 'PENDING')
  const decided = (proposals.data?.proposals ?? []).filter((proposal) => proposal.status !== 'PENDING')

  return (
    <div className="space-y-4">
      <Card
        title="Inject a scripted synthetic event"
        subtitle="Each event targets fictional hubs and assets. Injection produces proposals; it never activates a plan."
        tone="caution"
      >
        {scripted.error ? (
          <ErrorNote error={scripted.error} />
        ) : scripted.loading ? (
          <Loading />
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {(scripted.data?.events ?? []).map((event) => (
              <button
                key={event.key}
                type="button"
                onClick={() => void inject(event.key)}
                disabled={injecting !== null || !capabilities?.can_inject_events}
                data-testid={`inject-${event.key}`}
                title={
                  capabilities?.can_inject_events
                    ? event.description
                    : 'Your role cannot inject events'
                }
                className="rounded border border-edge bg-surface-sunken p-2 text-left text-xs transition-colors hover:border-accent/60 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <div className="flex items-center gap-2">
                  <Badge tone="caution">{event.key}</Badge>
                  <span className="font-semibold">{event.title}</span>
                </div>
                <p className="mt-1 text-[11px] leading-snug text-ink-muted">{event.description}</p>
                <p className="tabular mt-1 text-[10px] text-ink-faint">
                  kind {event.kind} · {injecting === event.key ? 'injecting…' : 'inject'}
                </p>
              </button>
            ))}
          </div>
        )}

        <p className="mt-3 text-[11px] text-ink-faint">
          Acting as <strong>{role}</strong>. Server capability:{' '}
          {capabilities?.can_inject_events ? 'may inject events' : 'read-only for events'}.
        </p>

        {outcome.kind === 'error' && (
          <div className="mt-2">
            <ErrorNote error={outcome.message} forbidden={outcome.forbidden} />
          </div>
        )}
        {outcome.kind === 'ok' && (
          <p className="mt-2 rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
            {outcome.message}
          </p>
        )}
      </Card>

      {injection && (
        <Card
          title={`Consequences of ${injection.event.label}`}
          subtitle={`Event ${injection.event.id} at scenario minute ${injection.event.occurred_minute}.`}
        >
          <p className="text-xs text-ink-muted">{injection.message}</p>

          <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
            <Badge tone="neutral">
              affected: {injection.affected_mission_ids.join(', ') || 'none'}
            </Badge>
            <Badge tone="accent">
              frozen and held: {injection.frozen_mission_ids.join(', ') || 'none'}
            </Badge>
            <Badge tone="neutral">parent plan {injection.parent_plan_id}</Badge>
            <Badge tone="neutral">{injection.distinct_variants} distinct variant(s)</Badge>
          </div>

          <OptionDistinctnessNote
            distinctness={injection}
            optionsShown={injection.proposals.length}
          />

          {injection.affected_explanations.length > 0 && (
            <ul className="mt-3 space-y-1 text-xs text-ink-muted">
              {injection.affected_explanations.map((line) => (
                <li key={line} className="border-l-2 border-caution/60 pl-2">
                  {line}
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <Card
        title={`Pending proposals (${pending.length})`}
        subtitle={
          proposals.data?.approval_note ??
          'Approval activates this synthetic plan version. Nothing activates on its own.'
        }
        tone="caution"
      >
        {proposals.loading && pending.length === 0 ? (
          <Loading />
        ) : proposals.error ? (
          <ErrorNote error={proposals.error} />
        ) : pending.length === 0 ? (
          <Empty>
            No pending proposals. Generate a plan set, then inject an event, or inject an event
            against the active plan.
          </Empty>
        ) : (
          <div className="space-y-3">
            {pending.map((proposal) => (
              <ProposalBlock
                key={proposal.id}
                proposal={proposal}
                canApprove={Boolean(capabilities?.can_approve)}
                busy={injecting !== null}
                onDecide={(kind) => void decide(proposal.id, kind, setOutcome, onChanged)}
              />
            ))}
          </div>
        )}
      </Card>

      {decided.length > 0 && (
        <Card title="Decided proposals" subtitle="Approval history for this scenario.">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
                <tr>
                  <th className="py-1">Proposal</th>
                  <th>Option</th>
                  <th>Status</th>
                  <th>Plan</th>
                  <th>Decided</th>
                  <th>Changes</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {decided.map((proposal) => (
                  <tr key={proposal.id} className="border-t border-edge align-top">
                    <td className="tabular py-1 font-semibold">{proposal.id}</td>
                    <td>{proposal.label}</td>
                    <td>
                      <Badge
                        tone={
                          proposal.status === 'APPROVED'
                            ? 'good'
                            : proposal.status === 'REJECTED'
                              ? 'bad'
                              : 'neutral'
                        }
                      >
                        {proposal.status_label ?? proposal.status}
                      </Badge>
                    </td>
                    <td className="tabular">{proposal.plan_id}</td>
                    <td className="tabular text-ink-faint">
                      {proposal.decided_at ?? '—'}
                      {proposal.decided_by ? ` (${proposal.decided_by})` : ''}
                    </td>
                    <td className="tabular">
                      {proposal.diff.changed.length + proposal.diff.added.length + proposal.diff.removed.length}
                    </td>
                    <td className="text-ink-muted">{proposal.decision_note || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}

async function decide(
  proposalId: string,
  kind: 'approve' | 'reject',
  setOutcome: (outcome: ActionOutcome) => void,
  onChanged: () => void,
): Promise<void> {
  setOutcome({ kind: 'idle' })
  try {
    const note = `Decided from the disruption centre as a synthetic demonstration action.`
    const response =
      kind === 'approve'
        ? await api.approve(proposalId, note)
        : await api.reject(proposalId, note)
    setOutcome({ kind: 'ok', message: response.message })
    onChanged()
  } catch (caught) {
    setOutcome(toOutcome(caught))
    // A conflict still means the server refused to act, so the view should refresh to show
    // the current pending set.
    onChanged()
  }
}

function toOutcome(caught: unknown): ActionOutcome {
  if (caught instanceof ApiError) {
    return {
      kind: 'error',
      message: caught.detail,
      forbidden: caught.isForbidden,
      conflict: caught.isConflict,
    }
  }
  return {
    kind: 'error',
    message: caught instanceof Error ? caught.message : 'Unexpected error.',
    forbidden: false,
    conflict: false,
  }
}

function ProposalBlock({
  proposal,
  canApprove,
  busy,
  onDecide,
}: {
  proposal: Proposal
  canApprove: boolean
  busy: boolean
  onDecide: (kind: 'approve' | 'reject') => void
}) {
  const [open, setOpen] = useState(false)
  const totalChanges =
    proposal.diff.changed.length + proposal.diff.added.length + proposal.diff.removed.length

  return (
    <article
      data-testid={`proposal-${proposal.id}`}
      className="rounded-lg border border-edge bg-surface-sunken"
    >
      <header className="flex flex-wrap items-center gap-3 border-b border-edge px-3 py-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold">{proposal.label}</span>
            {proposal.is_fallback && <Badge tone="caution">FALLBACK</Badge>}
            <Badge tone="neutral">rank {proposal.rank}</Badge>
          </div>
          <p className="tabular text-[11px] text-ink-faint">
            {proposal.id} · plan {proposal.plan_id} · trigger {proposal.trigger_event_id ?? 'manual'}
          </p>
        </div>

        <dl className="flex flex-wrap gap-4 text-xs">
          <Metric label="Weighted coverage" value={Number(proposal.metrics.weighted_coverage ?? 0).toFixed(0)} />
          <Metric label="Mean risk" value={Number(proposal.metrics.mean_risk ?? 0).toFixed(3)} />
          <Metric label="Changes" value={totalChanges} />
          <Metric label="Frozen held" value={proposal.diff.frozen_held_count} />
        </dl>

        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" onClick={() => setOpen((value) => !value)}>
            {open ? 'Hide diff' : 'Show diff'}
          </Button>
          <Button variant="danger" disabled={busy} onClick={() => onDecide('reject')} testId={`reject-${proposal.id}`}>
            Reject
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => onDecide('approve')}
            testId={`approve-${proposal.id}`}
            title={
              canApprove
                ? 'Activation requires the Commander role and a passing independent audit'
                : 'Only the COMMANDER role may approve. This will be refused by the API.'
            }
          >
            Approve &amp; activate
          </Button>
        </div>
      </header>

      <div className="px-3 py-2">
        <p className="text-[11px] text-caution">
          <strong>Approval activates this synthetic plan version.</strong>{' '}
          {canApprove
            ? 'You are acting as COMMANDER, so the server will accept this decision.'
            : 'The API will reject this for your role — that refusal is the feature, not a bug.'}
        </p>

        {proposal.key_reasons.length > 0 && (
          <ul className="mt-2 space-y-1 text-xs text-ink-muted">
            {proposal.key_reasons.map((reason) => (
              <li key={reason} className="border-l-2 border-accent/50 pl-2">
                {reason}
              </li>
            ))}
          </ul>
        )}

        {open && (
          <div className="mt-3 space-y-3">
            <DiffTable
              title={`Reassigned (${proposal.diff.changed.length})`}
              rows={proposal.diff.changed}
            />
            <DiffTable title={`Newly assigned (${proposal.diff.added.length})`} rows={proposal.diff.added} />
            <DiffTable
              title={`No longer assigned (${proposal.diff.removed.length})`}
              rows={proposal.diff.removed}
            />

            {proposal.explanations.length > 0 && (
              <details className="rounded border border-edge bg-surface px-3 py-2">
                <summary className="cursor-pointer text-xs font-semibold text-ink">
                  Per-mission explanations ({proposal.explanations.length})
                </summary>
                <ul className="mt-2 space-y-1 text-[11px] text-ink-muted">
                  {proposal.explanations.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </details>
            )}

            <p className="text-[11px] text-ink-faint">
              {proposal.diff.unchanged_count} assignment(s) unchanged. The plan is re-audited
              immediately before activation; if the independent auditor finds an error the
              server refuses to activate it.
            </p>
          </div>
        )}
      </div>
    </article>
  )
}

function DiffTable({ title, rows }: { title: string; rows: Proposal['diff']['changed'] }) {
  if (rows.length === 0) return null
  return (
    <div>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-faint">
        {title}
      </h4>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
            <tr>
              <th className="py-1 pr-2">Mission</th>
              <th className="pr-2">Asset</th>
              <th className="pr-2">Takeoff</th>
              <th className="pr-2">Risk</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.change}-${row.mission_id}-${row.asset_id}`} className="border-t border-edge">
                <td className="py-1 pr-2">
                  <span className="mr-1 inline-flex">
                    <PriorityPill priority={row.priority} />
                  </span>
                  <span className="tabular">{row.mission_id}</span>
                </td>
                <td className="tabular pr-2">
                  {row.previous_asset_id ? (
                    <>
                      <span className="text-ink-faint line-through">{row.previous_asset_id}</span>{' '}
                      → {row.asset_id}
                    </>
                  ) : (
                    row.asset_id
                  )}
                </td>
                <td className="tabular pr-2">
                  {row.previous_takeoff_minute !== undefined ? (
                    <>
                      {row.previous_takeoff_minute} → {row.takeoff_minute}
                      {row.takeoff_delta_minutes ? (
                        <span className="text-ink-faint">
                          {' '}
                          ({row.takeoff_delta_minutes > 0 ? '+' : ''}
                          {row.takeoff_delta_minutes})
                        </span>
                      ) : null}
                    </>
                  ) : (
                    row.takeoff_minute
                  )}
                </td>
                <td className="tabular pr-2">
                  {row.risk_delta !== undefined ? (
                    <>
                      {row.previous_risk?.toFixed(3)} → {row.risk.toFixed(3)}
                      <span className={row.risk_delta > 0 ? 'text-caution' : 'text-emerald-300'}>
                        {' '}
                        ({row.risk_delta > 0 ? '+' : ''}
                        {row.risk_delta.toFixed(3)})
                      </span>
                    </>
                  ) : (
                    row.risk.toFixed(3)
                  )}
                </td>
                <td className="text-ink-muted">{row.change}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}