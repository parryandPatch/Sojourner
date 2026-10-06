/**
 * Screen D — What-if lab.
 *
 * Runs an experiment on a *clone* of the operational state. This screen has no approve
 * control and must never gain one: the backend route writes a ledger entry describing the
 * experiment but persists no plan, and the response carries a digest pair proving the live
 * state digest is unchanged.
 *
 * The isolation proof is displayed rather than asserted: if the digests ever differ the UI
 * says so loudly, because that would be a real defect.
 */
import { useState } from 'react'

import { api, ApiError } from '../api/client'
import { Badge, Button, Card, Empty, ErrorNote, Loading, Metric } from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import { useRole } from '../hooks/useRole'
import type { WhatIfResponse } from '../types/api'
import type { RefreshTick } from '../App'

export function WhatIfLab({ tick }: { tick: RefreshTick }) {
  const { capabilities } = useRole()
  const templates = useAsync(() => api.whatIfTemplates(), [])
  const active = useAsync(() => api.overview(), [tick])

  const [eventKey, setEventKey] = useState('')
  const [variant, setVariant] = useState('COVERAGE FIRST')
  const [label, setLabel] = useState('what-if experiment')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<WhatIfResponse | null>(null)
  const [error, setError] = useState<{ message: string; forbidden: boolean } | null>(null)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const response = await api.whatIf({
        ...(eventKey ? { scripted_event: eventKey } : {}),
        variant,
        label,
      })
      setResult(response)
    } catch (caught) {
      setResult(null)
      setError({
        message: caught instanceof Error ? caught.message : 'What-if run failed.',
        forbidden: caught instanceof ApiError && caught.isForbidden,
      })
    } finally {
      setBusy(false)
    }
  }

  const variants = templates.data?.variants ?? ['COVERAGE FIRST', 'SAFETY FIRST', 'STABILITY FIRST']
  const scriptedKeys = templates.data?.scripted_events ?? []

  return (
    <div className="space-y-4">
      <Card
        title="Sandboxed experiment"
        subtitle="The experiment runs on a deep clone. The active plan, proposals, and the live state are untouched."
        actions={
          <Button
            onClick={() => void run()}
            disabled={busy || !capabilities?.can_run_what_if || !eventKey}
            variant="primary"
            testId="run-whatif"
            title={
              capabilities?.can_run_what_if
                ? 'Run the experiment on a cloned state'
                : 'Your role cannot run experiments'
            }
          >
            {busy ? 'Running…' : 'Run on cloned state'}
          </Button>
        }
      >
        {error && <ErrorNote error={error.message} forbidden={error.forbidden} />}

        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block text-xs">
            <span className="font-semibold uppercase tracking-wider text-ink-faint">
              Simulated event
            </span>
            <select
              value={eventKey}
              onChange={(event) => setEventKey(event.target.value)}
              className="mt-1 w-full rounded border border-edge-strong bg-surface-sunken px-2 py-1.5 text-xs"
              data-testid="whatif-event"
            >
              <option value="">Choose an event…</option>
              {scriptedKeys.map((key) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-xs">
            <span className="font-semibold uppercase tracking-wider text-ink-faint">
              Objective weighting
            </span>
            <select
              value={variant}
              onChange={(event) => setVariant(event.target.value)}
              className="mt-1 w-full rounded border border-edge-strong bg-surface-sunken px-2 py-1.5 text-xs"
            >
              {variants.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-xs">
            <span className="font-semibold uppercase tracking-wider text-ink-faint">Label</span>
            <input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              className="mt-1 w-full rounded border border-edge-strong bg-surface-sunken px-2 py-1.5 text-xs"
            />
          </label>
        </div>

        <p className="mt-3 text-[11px] leading-relaxed text-ink-faint">
          The API also accepts a JSON <code>overrides</code> object for hand-built field edits
          (for example <code>{'{"asset.AST-006": {"status": "UNAVAILABLE"}}'}</code>). This
          prototype's form covers the scripted-event path only; see{' '}
          <code>GET /api/v1/what-if/templates</code> for the override schema.
        </p>
      </Card>

      {result && (
        <Card
          title={`Result — ${result.label}`}
          subtitle={`Weighted as ${result.variant}. Solved in ${result.solve_time_ms} ms.`}
          tone={result.isolation_verified ? 'accent' : 'caution'}
        >
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={result.isolation_verified ? 'good' : 'bad'}>
              {result.isolation_verified ? 'ISOLATION VERIFIED' : 'ISOLATION FAILED'}
            </Badge>
            <Badge tone={result.experiment_auditor_valid ? 'good' : 'bad'}>
              {result.experiment_auditor_valid
                ? 'EXPERIMENT AUDITOR-VALID'
                : 'EXPERIMENT AUDITOR-INVALID'}
            </Badge>
            <span className="text-ink-faint">{result.approval_notice}</span>
          </div>

          <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Metric
              label="Coverage delta"
              value={result.coverage_delta.toFixed(1)}
              detail="weighted coverage, experiment minus active"
            />
            <Metric
              label="Mean risk delta"
              value={result.mean_risk_delta.toFixed(4)}
              detail="positive means riskier than the active plan"
            />
            <Metric label="Missions changed" value={result.changed_missions.length} />
            <Metric label="Solve time" value={`${result.solve_time_ms} ms`} />
            <Metric
              label="Active plan"
              value={result.active_plan_id ?? 'none'}
              detail="unchanged by this run"
            />
          </dl>

          <div className="mt-3 grid gap-2 text-[11px] sm:grid-cols-2">
            <div className="rounded border border-edge bg-surface-sunken px-3 py-2">
              <p className="font-semibold uppercase tracking-wider text-ink-faint">
                Live state digest before
              </p>
              <p className="tabular mt-1 break-all text-ink-muted">{result.state_digest_before}</p>
            </div>
            <div className="rounded border border-edge bg-surface-sunken px-3 py-2">
              <p className="font-semibold uppercase tracking-wider text-ink-faint">
                Live state digest after
              </p>
              <p className="tabular mt-1 break-all text-ink-muted">{result.state_digest_after}</p>
            </div>
          </div>

          {result.notes.length > 0 && (
            <ul className="mt-3 space-y-1 text-[11px] text-caution">
              {result.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}

          {!result.experiment_auditor_valid && (
            <ul className="mt-3 list-disc pl-5 text-xs text-rose-200">
              {result.experiment_auditor_findings.map((finding) => (
                <li key={finding}>{finding}</li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {result && (
        <Card
          title="Side by side: active plan versus experiment"
          subtitle="Only the mission rows that differ are shown, but the underlying comparison covers every assigned mission."
        >
          {result.comparison.filter((row) => row.changed).length === 0 ? (
            <Empty>
              The experiment produced exactly the same assignments as the active plan. The
              simulated event had no effect on this objective weighting.
            </Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-[10px] uppercase tracking-wider text-ink-faint">
                  <tr>
                    <th className="py-1 pr-2">Mission</th>
                    <th className="pr-2">Active asset</th>
                    <th className="pr-2">Active takeoff</th>
                    <th className="pr-2">Experiment asset</th>
                    <th>Experiment takeoff</th>
                  </tr>
                </thead>
                <tbody>
                  {result.comparison
                    .filter((row) => row.changed)
                    .map((row) => (
                      <tr key={row.mission_id} className="border-t border-edge">
                        <td className="tabular py-1 pr-2 font-semibold">{row.mission_id}</td>
                        <td className="tabular pr-2 text-ink-faint">
                          {row.active_asset_id ?? 'unassigned'}
                        </td>
                        <td className="tabular pr-2 text-ink-faint">
                          {row.active_takeoff ?? '—'}
                        </td>
                        <td className="tabular pr-2 text-accent">
                          {row.experiment_asset_id ?? 'unassigned'}
                        </td>
                        <td className="tabular text-accent">{row.experiment_takeoff ?? '—'}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      <Card title="Why this screen has no approve button" subtitle="By design, enforced in code.">
        <p className="text-xs leading-relaxed text-ink-muted">
          A what-if run is information. It clones the operational state, applies the experiment,
          solves, audits the result, and throws the plan away. The only persistent trace is a
          ledger entry recording that the experiment happened — including the digest pair that
          proves the live state was untouched. Activating a plan is a separate action, taken by
          a Commander on the disruption centre, and re-audited at that moment.
        </p>
        {active.data && (
          <p className="mt-2 text-[11px] text-ink-faint">
            Currently active plan: {active.data.active_plan?.id ?? 'none approved'},{' '}
            {active.data.pending_proposals} proposal(s) pending.
          </p>
        )}
        {templates.error && <ErrorNote error={templates.error} />}
        {templates.loading && !templates.data && <Loading label="Loading templates…" />}
      </Card>
    </div>
  )
}