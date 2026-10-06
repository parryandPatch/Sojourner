/**
 * Thin typed fetch wrapper.
 *
 * The base URL is relative: in development Vite proxies /api and /ws to the backend, and
 * in production nginx does the same, so there is no environment-specific URL to configure.
 *
 * The role travels in the `X-SOJOURNER-Role` header. This is a UI convenience only — the
 * backend enforces the same rules server-side, and a 403 here is expected behaviour for a
 * non-Commander trying to approve, not a bug to work around.
 */
import type {
  AssetRow,
  ClockResponse,
  FeasibilityResponse,
  GeneratePlansResponse,
  InjectEventResponse,
  LedgerResponse,
  LedgerVerification,
  MapPayload,
  Mission,
  Overview,
  Plan,
  Proposal,
  ReadinessResponse,
  ReasonCodesResponse,
  ReplayResponse,
  Role,
  ScriptedEvent,
  WhatIfResponse,
  WhatIfTemplates,
} from '../types/api'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail)
    this.name = 'ApiError'
  }

  /** A 403 is the role guard working correctly; the UI should explain it, not retry. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  get isConflict(): boolean {
    return this.status === 409
  }
}

let currentRole: Role = 'OBSERVER'

export function setRole(role: Role): void {
  currentRole = role
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-SOJOURNER-Role': currentRole,
      ...(init.headers ?? {}),
    },
  })

  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = body.detail ?? body.message ?? detail
    } catch {
      // Non-JSON error body: keep the status line.
    }
    throw new ApiError(response.status, String(detail))
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

const post = <T>(path: string, body: unknown = {}) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

/** The backend serialises the asset class as `class_` (`class` is a Python keyword). */
function toAssetRow(raw: Record<string, unknown>): AssetRow {
  const { class_, ...rest } = raw as Record<string, unknown> & { class_?: unknown }
  return { ...rest, class: String(class_ ?? rest.status ?? 'UNKNOWN') } as AssetRow
}

export const api = {
  overview: () => request<Overview>('/state/overview'),
  map: () => request<MapPayload>('/state/map'),
  readiness: () =>
    request<ReadinessResponse>('/state/readiness').then((response) => ({
      ...response,
      assets: response.assets.map((asset) => toAssetRow(asset as unknown as Record<string, unknown>)),
    })),
  crew: (hubId?: string) =>
    request<{ crew: Array<Record<string, unknown>>; counts: Record<string, number> }>(
      `/state/crew${hubId ? `?hub_id=${hubId}` : ''}`,
    ),
  reasonCodes: () => request<ReasonCodesResponse>('/state/reason-codes'),
  advanceClock: (advanceToMinute: number) =>
    post<ClockResponse>('/state/clock', { advance_to_minute: advanceToMinute }),

  missions: () => request<{ missions: Mission[]; total?: number }>('/missions'),
  feasibility: (missionId: string) => request<FeasibilityResponse>(`/feasibility/${missionId}`),
  assets: () =>
    request<{ assets: Array<Record<string, unknown>>; total?: number }>('/assets').then((response) => ({
      ...response,
      assets: response.assets.map(toAssetRow),
    })),

  generatePlans: (revise = false) =>
    post<GeneratePlansResponse>('/plans/generate', { revise }),
  plans: () => request<{ plans: Plan[]; active_plan_id: string | null }>('/plans'),
  plan: (planId: string) => request<Plan>(`/plans/${planId}`),
  planExplanations: (planId: string) =>
    request<{ plan_id: string; variant: string; explanations: string[]; reason_codes: Array<{ code: string; label: string }> }>(
      `/plans/${planId}/explanations`,
    ),

  scriptedEvents: () => request<{ events: ScriptedEvent[]; synthetic_data_notice: string }>('/events/scripted'),
  events: () => request<{ events: Array<Record<string, unknown>> }>('/events'),
  injectEvent: (scriptedEvent: string) =>
    post<InjectEventResponse>('/events/inject', { scripted_event: scriptedEvent }),

  proposals: () =>
    request<{ proposals: Proposal[]; active_plan_id: string | null; approval_note: string }>('/proposals'),
  proposal: (proposalId: string) => request<Proposal>(`/proposals/${proposalId}`),
  approve: (proposalId: string, note: string) =>
    post<{ message: string; plan_id: string; status: string; note?: string }>(
      `/proposals/${proposalId}/approve`,
      { note },
    ),
  reject: (proposalId: string, note: string) =>
    post<{ message: string }>(`/proposals/${proposalId}/reject`, { note }),

  whatIf: (body: {
    scripted_event?: string
    scripted_events?: string[]
    variant?: string
    label?: string
    overrides?: Record<string, unknown>
  }) => post<WhatIfResponse>('/what-if/run', body),
  whatIfTemplates: () => request<WhatIfTemplates>('/what-if/templates'),

  ledger: () => request<LedgerResponse>('/ledger'),
  verifyLedger: () => request<LedgerVerification>('/ledger/verify'),
  replay: (planId: string) => request<ReplayResponse>(`/replay/${planId}`),

  resetScenario: () => post<Record<string, unknown>>('/scenarios/reset'),
}

export function liveSocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/live`
}