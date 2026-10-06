/**
 * Screen E — Decision trail.
 *
 * The hash-chained ledger plus a replay control. The chain status is recomputed on demand
 * rather than trusted from the stored value, so a reviewer can watch VALID become INVALID by
 * mutating a row underneath the UI (the backend exposes no mutation route for this — the
 * test suite tampers directly in the database, which is the honest way to demo it).
 *
 * Per the spec the entry hash covers `previous_hash + canonical_JSON(payload) +
 * occurred_at + action + actor_role`. `description` and `entity_ref` sit outside that formula,
 * so editing one of those is not detectable by the chain. The UI states this rather than
 * implying a stronger guarantee than the code provides.
 */
import { useState } from 'react'

import { api } from '../api/client'
import { Badge, Button, Card, Empty, ErrorNote, Loading } from '../components/ui'
import { useAsync } from '../hooks/useAsync'
import type { LedgerEntry, ReplayStep } from '../types/api'
import type { RefreshTick } from '../App'

const ACTION_TONE: Record<string, 'neutral' | 'accent' | 'caution' | 'good' | 'bad'> = {
  SCENARIO_GENERATED: 'neutral',
  SCENARIO_RESET: 'neutral',
  PLAN_GENERATED: 'accent',
  EVENT_INJECTED: 'caution',
  PROPOSALS_GENERATED: 'caution',
  PROPOSAL_APPROVED: 'good',
  PROPOSAL_REJECTED: 'bad',
  WHAT_IF_RUN: 'neutral',
  CLOCK_ADVANCED: 'neutral',
}

export function DecisionTrailScreen({ tick }: { tick: RefreshTick }) {
  const ledger = useAsync(() => api.ledger(), [tick])
  const [expanded, setExpanded] = useState<number | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkNote, setCheckNote] = useState<string | null>(null)
  const [replayPlanId, setReplayPlanId] = useState<string | null>(null)

  const verify = async () => {
    setChecking(true)
    setCheckNote(null)
    try {
      const result = await api.verifyLedger()
      setCheckNote(
        result.chain_status === 'VALID'
          ? `Recomputed ${result.checked} hash(es): chain is VALID. Head ${shorten(result.head_hash)}`
          : `Chain INVALID at sequence ${result.broken_at ?? '?'} — ${result.checked} hash(es) checked.`,
      )
    } catch (caught) {
      setCheckNote(caught instanceof Error ? caught.message : 'Verification failed.')
    } finally {
      setChecking(false)
    }
  }

  if (ledger.loading && !ledger.data) return <Loading label="Loading decision trail…" />
  if (ledger.error && !ledger.data) return <ErrorNote error={ledger.error} />
  if (!ledger.data) return <Empty>Ledger unavailable.</Empty>

  // Narrowed above the early returns; bound to a local so the closure below is not a
  // possibly-null access under strict mode.
  const trail = ledger.data
  const entries = [...trail.entries].reverse()

  return (
    <div className="space-y-4">
      <Card
        title="Hash chain status"
        subtitle="Every key action appends one record. Each record hashes its predecessor."
        tone={trail.chain_status === 'VALID' ? 'accent' : 'caution'}
        actions={
          <Button onClick={() => void verify()} disabled={checking} testId="verify-chain">
            {checking ? 'Verifying…' : 'Re-verify chain'}
          </Button>
        }
      >
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <Badge tone={trail.chain_status === 'VALID' ? 'good' : 'bad'}>
            {trail.chain_badge}
          </Badge>
          <span className="tabular">{trail.chain_length} record(s)</span>
          <span className="tabular text-ink-faint">head {shorten(trail.head_hash)}</span>
          {checkNote && <span className="text-ink-muted">{checkNote}</span>}
        </div>

        <p className="mt-3 rounded border border-edge bg-surface-sunken px-3 py-2 text-[11px] leading-relaxed text-ink-muted">
          {trail.integrity_note}
          <br />
          <strong>Scope limit:</strong> the hash covers{' '}
          <code>previous_hash + canonical_JSON(payload) + occurred_at + action + actor_role</code>.
          The human-readable <code>description</code> and <code>entity_ref</code> columns are not
          inside that formula, so editing one of them would not be detected by this chain. That is
          a known limitation of the spec's hash definition, documented in{' '}
          <code>docs/LIMITATIONS.md</code>.
        </p>
      </Card>

      <Card title="Decision records" subtitle="Newest first. Click a row to inspect its payload.">
        {entries.length === 0 ? (
          <Empty>The ledger is empty. Reset the scenario to seed the genesis record.</Empty>
        ) : (
          <ol className="space-y-1">
            {entries.map((entry) => (
              <LedgerRow
                key={entry.sequence}
                entry={entry}
                isHead={entry.sequence === trail.chain_length}
                isOpen={expanded === entry.sequence}
                onToggle={() =>
                  setExpanded((current) => (current === entry.sequence ? null : entry.sequence))
                }
              />
            ))}
          </ol>
        )}
      </Card>

      <Card
        title="Replay"
        subtitle="Step through the recorded decisions that produced a plan version. Reading the replay changes nothing."
        actions={
          <select
            value={replayPlanId ?? ''}
            onChange={(event) => setReplayPlanId(event.target.value || null)}
            className="rounded border border-edge-strong bg-surface-sunken px-2 py-1 text-xs"
            data-testid="replay-plan"
          >
            <option value="">Choose a plan version…</option>
            {trail.entries
              .flatMap((entry) => planIdsOf(entry))
              .filter((id, index, all) => all.indexOf(id) === index)
              .sort()
              .reverse()
              .map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
          </select>
        }
      >
        {replayPlanId ? (
          <ReplayPanel planId={replayPlanId} />
        ) : (
          <Empty>Select a plan version to load its recorded decision sequence.</Empty>
        )}
      </Card>
    </div>
  )
}

function planIdsOf(entry: LedgerEntry): string[] {
  const ids = new Set<string>()
  for (const chunk of entry.entity_ref.split(',')) {
    const trimmed = chunk.trim()
    if (/^PLN-/.test(trimmed)) ids.add(trimmed)
  }
  const payload = entry.payload as Record<string, unknown>
  const listed = payload['plan_ids']
  if (Array.isArray(listed)) {
    for (const value of listed) if (typeof value === 'string') ids.add(value)
  }
  return [...ids]
}

function LedgerRow({
  entry,
  isHead,
  isOpen,
  onToggle,
}: {
  entry: LedgerEntry
  isHead: boolean
  isOpen: boolean
  onToggle: () => void
}) {
  const tone = ACTION_TONE[entry.action] ?? 'neutral'
  return (
    <li className="rounded border border-edge bg-surface-sunken">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="flex w-full flex-wrap items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-surface"
      >
        <span className="tabular w-8 shrink-0 text-ink-faint">#{entry.sequence}</span>
        <Badge tone={tone}>{entry.action}</Badge>
        <span className="text-ink-muted">{entry.description}</span>
        <span className="tabular ml-auto text-ink-faint">
          {entry.actor_role} · {entry.occurred_at}
        </span>
        {isHead && <Badge tone="accent" title="Latest entry in the chain">HEAD</Badge>}
      </button>

      {isOpen && (
        <div className="border-t border-edge px-3 py-2 text-[11px]">
          <dl className="grid gap-1 sm:grid-cols-2">
            <div>
              <dt className="font-semibold uppercase tracking-wider text-ink-faint">Entry hash</dt>
              <dd className="tabular break-all text-ink-muted">{entry.entry_hash}</dd>
            </div>
            <div>
              <dt className="font-semibold uppercase tracking-wider text-ink-faint">
                Previous hash
              </dt>
              <dd className="tabular break-all text-ink-muted">{entry.previous_hash}</dd>
            </div>
            <div>
              <dt className="font-semibold uppercase tracking-wider text-ink-faint">Entity ref</dt>
              <dd className="tabular break-all text-ink-muted">{entry.entity_ref || '—'}</dd>
            </div>
            <div>
              <dt className="font-semibold uppercase tracking-wider text-ink-faint">Actor role</dt>
              <dd className="tabular text-ink-muted">{entry.actor_role}</dd>
            </div>
          </dl>
          <pre className="tabular mt-2 max-h-48 overflow-auto rounded bg-surface-sunken p-2 text-ink-faint">
            {JSON.stringify(entry.payload, null, 2)}
          </pre>
        </div>
      )}
    </li>
  )
}

function ReplayPanel({ planId }: { planId: string }) {
  const [step, setStep] = useState(0)
  const replay = useAsync(() => api.replay(planId), [planId])

  if (replay.loading) return <Loading label="Loading replay…" />
  if (replay.error) return <ErrorNote error={replay.error} />
  if (!replay.data) return <Empty>No replay available.</Empty>

  const steps: ReplayStep[] = replay.data.steps
  const current = steps[Math.min(step, steps.length - 1)]

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold">
          {replay.data.plan_id} · version {replay.data.plan_version} · {replay.data.plan_variant}
        </span>
        <Badge tone={replay.data.chain_status === 'VALID' ? 'good' : 'bad'}>
          CHAIN {replay.data.chain_status}
        </Badge>
        <span className="text-ink-faint">{replay.data.note}</span>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <Button onClick={() => setStep((value) => Math.max(0, value - 1))} disabled={step === 0}>
          ◀ Previous
        </Button>
        <Button
          onClick={() => setStep((value) => Math.min(steps.length - 1, value + 1))}
          disabled={step >= steps.length - 1}
          variant="primary"
        >
          Next ▶
        </Button>
        <Button onClick={() => setStep(steps.length - 1)} disabled={step >= steps.length - 1}>
          Jump to end
        </Button>
        <span className="tabular text-ink-faint">
          step {steps.length === 0 ? 0 : step + 1} of {steps.length}
        </span>
      </div>

      <ol className="mt-3 space-y-0.5">
        {steps.map((entry, index) => (
          <li
            key={entry.sequence}
            className={`flex items-center gap-2 rounded px-2 py-1 text-[11px] ${
              index === step ? 'bg-accent/10 text-ink' : 'text-ink-faint'
            }`}
          >
            <span className="tabular w-8">#{entry.sequence}</span>
            <Badge tone={ACTION_TONE[entry.action] ?? 'neutral'}>{entry.action}</Badge>
            <span className="truncate">{entry.description}</span>
            {entry.is_on_plan_path && (
              <Badge tone="accent" title="This record references the selected plan version">
                ON PATH
              </Badge>
            )}
          </li>
        ))}
      </ol>

      {current && (
        <pre className="tabular mt-3 max-h-56 overflow-auto rounded bg-surface-sunken p-3 text-[11px] text-ink-muted">
          {JSON.stringify(
            {
              sequence: current.sequence,
              occurred_at: current.occurred_at,
              action: current.action,
              actor_role: current.actor_role,
              entity_ref: current.entity_ref,
              description: current.description,
              entry_hash: current.entry_hash,
              payload: current.payload,
            },
            null,
            2,
          )}
        </pre>
      )}
    </div>
  )
}

function shorten(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 8)}…${hash.slice(-8)}` : hash
}