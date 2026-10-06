/** Small presentational primitives shared by the six screens. */
import type { ReactNode } from 'react'

export function Card({
  title,
  subtitle,
  actions,
  children,
  tone = 'neutral',
}: {
  title?: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'caution'
}) {
  const toneClass =
    tone === 'accent'
      ? 'border-accent/40'
      : tone === 'caution'
        ? 'border-caution/40'
        : 'border-edge'
  return (
    <section className={`rounded-lg border ${toneClass} bg-surface-raised`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-2.5">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-wide text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-faint">{subtitle}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Button({
  children,
  onClick,
  disabled,
  variant = 'default',
  title,
  testId,
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  variant?: 'default' | 'primary' | 'danger' | 'ghost'
  title?: string
  testId?: string
}) {
  const base =
    'rounded border px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40'
  const variants: Record<string, string> = {
    default: 'border-edge-strong bg-surface-sunken text-ink hover:border-accent/60',
    primary: 'border-accent/60 bg-accent/15 text-accent hover:bg-accent/25',
    danger: 'border-caution/60 bg-caution/15 text-caution hover:bg-caution/25',
    ghost: 'border-transparent bg-transparent text-ink-muted hover:text-ink',
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      data-testid={testId}
      className={`${base} ${variants[variant]}`}
    >
      {children}
    </button>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'caution' | 'good' | 'bad'
  title?: string
}) {
  const tones: Record<string, string> = {
    neutral: 'border-edge-strong bg-surface-sunken text-ink-muted',
    accent: 'border-accent/50 bg-accent/10 text-accent',
    caution: 'border-caution/50 bg-caution/10 text-caution',
    good: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300',
    bad: 'border-rose-500/50 bg-rose-500/10 text-rose-300',
  }
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tones[tone]}`}
    >
      {children}
    </span>
  )
}

/** Priority pill. P1 is highest priority and is always the loudest. */
export function PriorityPill({ priority }: { priority: number }) {
  const tones: Record<number, string> = {
    1: 'bg-rose-500/25 text-rose-200 border-rose-400/50',
    2: 'bg-caution/20 text-caution border-caution/40',
    3: 'bg-accent/15 text-accent border-accent/40',
    4: 'bg-edge-strong/40 text-ink-muted border-edge-strong',
    5: 'bg-edge-strong/25 text-ink-faint border-edge',
  }
  return (
    <span
      className={`inline-flex h-5 w-5 items-center justify-center rounded border text-[10px] font-bold ${tones[priority] ?? tones[5]}`}
      title={`Priority ${priority} (P1 is highest)`}
    >
      P{priority}
    </span>
  )
}

/**
 * Risk readout. Always numeric plus a band, never colour alone, so the value survives
 * greyscale and screen readers.
 */
export function RiskBar({ value, label }: { value: number; label?: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-full overflow-hidden rounded bg-surface-sunken"
        role="meter"
        aria-valuenow={Number(value.toFixed(3))}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-label={label ?? 'combined risk'}
      >
        <div
          className="h-full rounded bg-advisory"
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
      </div>
      <span className="tabular w-10 shrink-0 text-right text-xs text-ink-muted">
        {value.toFixed(3)}
      </span>
    </div>
  )
}

export function Metric({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <div>
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">{label}</dt>
      <dd className="tabular mt-0.5 text-sm font-semibold text-ink">{value}</dd>
      {detail && <p className="mt-0.5 text-[11px] leading-tight text-ink-faint">{detail}</p>}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded border border-dashed border-edge px-3 py-6 text-center text-xs text-ink-faint">
      {children}
    </p>
  )
}

export function ErrorNote({ error, forbidden }: { error: string; forbidden?: boolean }) {
  return (
    <div
      role="alert"
      className="rounded border border-caution/40 bg-caution/10 px-3 py-2 text-xs text-caution"
    >
      {forbidden ? (
        <>
          <strong className="font-semibold">Not permitted for this role.</strong> {error}
        </>
      ) : (
        error
      )}
    </div>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <p className="px-1 py-4 text-xs text-ink-faint" role="status">
      {label}
    </p>
  )
}
