/**
 * Response shapes from the SOJOURNER backend.
 *
 * These mirror the FastAPI route return values field-for-field. The backend is the
 * source of truth; if a field is missing here, the UI does not use it. Nothing in this
 * file describes a real system, place, or unit — every entity is synthetic.
 *
 * Note the deliberate `class_` spelling: the backend serialises the asset class under
 * `class_` because `class` is a Python keyword. The UI renames it to `class` at the
 * boundary in `AssetRow`.
 */

export type Role = 'OBSERVER' | 'PLANNER' | 'MAINTAINER' | 'COMMANDER'

export interface RoleCapabilities {
  can_view: boolean
  can_write: boolean
  can_generate_plans: boolean
  can_inject_events: boolean
  can_run_what_if: boolean
  can_approve: boolean
  capabilities: string[]
}

/**
 * The two labels that must be visible on every screen, from the spec. These are
 * duplicated in the shell rather than fetched, so a reviewer can see at a glance that
 * they are hard requirements rather than a rendering of some backend field.
 */
export const SYNTHETIC_NOTICE = 'SYNTHETIC DEMONSTRATION DATA'
export const ADVISORY_NOTICE = 'ADVISORY ONLY — HUMAN APPROVAL REQUIRED'

export interface RiskBreakdown {
  hazard_risk: number
  weather_risk: number
  readiness_risk: number
  combined: number
  formula: string
}

export interface PlanMetrics {
  missions_total: number
  missions_covered: number
  p1_covered: number
  p1_total: number
  weighted_coverage: number
  coverage_ratio: number
  mean_risk: number
  max_risk: number
  changed_assignments: number
  frozen_assignments: number
  risk_savings_vs_coverage_first: number
}

export interface Assignment {
  id: string
  mission_id: string
  asset_id: string
  crew_ids: string[]
  payload_category: string | null
  payload_units: number
  takeoff_minute: number
  landing_minute: number
  return_minute: number
  transit_minutes: number
  takeoff_iso: string
  landing_iso: string
  return_iso: string
  risk: number
  risk_breakdown: RiskBreakdown
  status: string
  is_frozen: boolean
  reason_codes: string[]
  explanation: string
}

export interface Plan {
  id: string
  version: number
  status: string
  status_label?: string
  variant: string
  is_fallback: boolean
  solver_status: string
  solve_time_ms: number
  created_at: string
  parent_plan_id: string | null
  approved_at: string | null
  approved_by: string | null
  metrics: PlanMetrics
  assignments: Assignment[]
  unassigned_mission_ids: string[]
  auditor_valid: boolean
  auditor_findings: string[]
  notes: string[]
  key_reasons?: string[]
  rank?: number
}

/**
 * How many genuinely different options the solver produced.
 *
 * The planner always returns three options, but it does not always return three
 * *different* ones: after a disruption it re-opens only the affected missions, and a
 * single asset fault can leave a single decision — at which point every objective
 * genuinely picks the same answer. When that happens the prototype says so rather
 * than perturbing a plan until it looks different, because a perturbed plan would
 * carry a label for an objective nobody optimised.
 */
export interface OptionDistinctness {
  /** How many of the returned options have different assignments. */
  distinct_variants: number
  /** Groups of option labels that produced identical assignments. */
  duplicate_options: string[][]
  /** How many missions the solve was actually free to decide. */
  free_mission_count: number
  /** Plain-language explanation of the count above. */
  distinct_options_note: string
}

export interface GeneratePlansResponse extends OptionDistinctness {
  parent_plan_id: string | null
  trigger_event_id: string | null
  coverage_reference: number | null
  plans: Plan[]
  solve_time_ms_total: number
  activation_note: string
  synthetic_data_notice: string
  advisory_notice: string
}

export interface KpiTile {
  key: string
  label: string
  value: string
  detail: string
  tone: 'neutral' | 'positive' | 'warning' | 'critical'
}

export interface OverviewEvent {
  id: string
  kind: string
  occurred_at: string
  occurred_minute: number
  label: string
  payload: Record<string, unknown>
  status: string
  source: string
  affected_mission_ids: string[]
}

export interface ReadinessAlert {
  asset_id: string
  label: string
  severity: 'HIGH' | 'MEDIUM' | 'LOW' | string
  message: string
  readiness_probability: number
}

export interface TimelineRow {
  mission_id: string
  mission_title: string
  priority: number
  asset_id: string
  crew_ids: string[]
  start_minute: number
  end_minute: number
  start_iso: string
  end_iso: string
  risk: number
  phase: 'PLANNED' | 'AIRBORNE' | 'DEPARTED' | string
  is_frozen: boolean
}

export interface Overview {
  scenario_id: string
  scenario_name: string
  synthetic_data_notice: string
  advisory_notice: string
  now_minute: number
  now_iso: string
  horizon_minutes: number
  active_plan: Plan | null
  pending_proposals: number
  kpis: KpiTile[]
  counts: Record<string, number>
  recent_events: OverviewEvent[]
  readiness_alerts: ReadinessAlert[]
  affected_mission_ids: string[]
  chain_status: string
  timeline: TimelineRow[]
  role_matrix: Record<string, RoleCapabilities>
  acting_role: Role
}

export interface Hub {
  id: string
  name: string
  x: number
  y: number
  runway_capacity_per_slot: number
  status: string
  slot_minutes: number
}

export interface AssetRow {
  id: string
  label: string
  class: string
  home_hub_id: string
  status: string
  available_from_minute: number
  cruise_speed: number
  endurance_minutes: number
  range_units: number
  maintenance_hours_since: number
  recent_fault_count: number
  readiness_probability: number
  readiness_low: number
  readiness_high: number
  readiness_method: string
  readiness_band: string
  assigned_mission_id: string | null
  tone?: string
}

export interface Mission {
  id: string
  title: string
  mission_kind: string
  priority: number
  priority_weight: number
  required_class: string
  required_payload_category: string | null
  required_asset_count: number
  origin_hub_id: string
  objective_x: number
  objective_y: number
  earliest_start: number
  latest_end: number
  station_duration_minutes: number
  maximum_risk: number
  required_crew_roles: string[]
  status: string
  region_id: string | null
  description: string
  earliest_start_iso: string
  latest_end_iso: string
  assigned_asset_id?: string | null
}

export interface HazardZone {
  id: string
  name: string
  cells: Array<[number, number]>
  severity: number
  band: string
  blocking: boolean
  active_start: number
  active_end: number
  source_event_id: string | null
}

export interface WeatherWindow {
  id: string
  name: string
  hub_id: string | null
  region_id: string | null
  start: number
  end: number
  severity: number
  restriction_type: string
  blocking: boolean
  source_event_id: string | null
}

export interface MapAssignment {
  mission_id: string
  asset_id: string
  crew_ids: string[]
  takeoff_minute: number
  return_minute: number
  is_frozen: boolean
  risk: number
}

export interface MapPayload {
  width_units: number
  height_units: number
  grid_cell: number
  now_minute: number
  hubs: Hub[]
  assets: Array<AssetRow & { x: number; y: number; is_airborne: boolean }>
  missions: Mission[]
  hazard_zones: HazardZone[]
  weather_windows: WeatherWindow[]
  assignments: MapAssignment[]
  legend: Array<{ key: string; label: string }>
  synthetic_data_notice: string
}

export interface ScriptedEvent {
  key: string
  title: string
  kind: string
  description: string
  payload_preview: Record<string, unknown>
}

export interface DiffRow {
  mission_id: string
  mission_title: string
  priority: number
  asset_id: string
  asset_class: string
  crew_ids: string[]
  takeoff_minute: number
  takeoff_iso: string
  return_minute: number
  return_iso: string
  risk: number
  risk_breakdown: RiskBreakdown
  change: string
  previous_asset_id?: string
  previous_takeoff_minute?: number
  previous_crew_ids?: string[]
  previous_risk?: number
  risk_delta?: number
  takeoff_delta_minutes?: number
}

export interface ProposalDiff {
  added: DiffRow[]
  removed: DiffRow[]
  changed: DiffRow[]
  unchanged_count: number
  frozen_held_count: number
}

export interface Proposal {
  id: string
  parent_plan_id: string | null
  trigger_event_id: string | null
  plan_id: string
  rank: number
  label: string
  is_fallback: boolean
  status: string
  status_label?: string
  created_at: string
  decided_at: string | null
  decided_by: string | null
  decision_note: string | null
  metrics: Partial<PlanMetrics>
  diff: ProposalDiff
  explanations: string[]
  key_reasons: string[]
  approval_warning: string
  plan?: Plan | null
  auditor_valid?: boolean
  auditor_findings?: string[]
}

export interface InjectEventResponse extends OptionDistinctness {
  event: OverviewEvent
  affected_mission_ids: string[]
  frozen_mission_ids: string[]
  parent_plan_id: string
  proposals: Proposal[]
  proposals_generated: boolean
  requires_commander_approval: boolean
  message: string
  affected_explanations: string[]
  advisory_notice: string
}

export interface LedgerEntry {
  id: number
  sequence: number
  occurred_at: string
  actor_role: Role
  action: string
  entity_ref: string
  description: string
  payload: Record<string, unknown>
  previous_hash: string
  entry_hash: string
}

export interface LedgerResponse {
  scenario_id: string
  entries: LedgerEntry[]
  chain_status: string
  chain_badge: string
  chain_length: number
  head_hash: string
  verification_detail: Record<string, unknown>
  integrity_note: string
}

export interface LedgerVerification {
  chain_status: string
  chain_badge: string
  chain_length: number
  checked: number
  broken_at: number | null
  head_hash: string
  verification_detail: Record<string, unknown>
}

export interface ReplayStep {
  sequence: number
  occurred_at: string
  action: string
  actor_role: Role
  entity_ref: string
  description: string
  payload: Record<string, unknown>
  entry_hash: string
  is_on_plan_path: boolean
}

export interface ReplayResponse {
  plan_id: string
  scenario_id: string
  plan_version: number
  plan_status: string
  plan_variant: string
  steps: ReplayStep[]
  chain_status: string
  head_hash: string
  plan_path_chain: string
  note: string
}

export interface WhatIfComparisonRow {
  mission_id: string
  active_asset_id: string | null
  experiment_asset_id: string | null
  active_takeoff: number | null
  experiment_takeoff: number | null
  changed: boolean
}

export interface WhatIfResponse {
  label: string
  variant: string
  state_digest_before: string
  state_digest_after: string
  isolation_verified: boolean
  active_plan_id: string | null
  experiment_plan: Plan
  experiment_auditor_valid: boolean
  experiment_auditor_findings: string[]
  comparison: WhatIfComparisonRow[]
  coverage_delta: number
  mean_risk_delta: number
  changed_missions: string[]
  notes: string[]
  solve_time_ms: number
  approval_notice: string
  advisory_notice: string
}

export interface WhatIfTemplates {
  variants: string[]
  scripted_events: string[]
  hubs: Array<{ id: string; name: string; status: string }>
  assets: Array<{ id: string; label: string; class: string; status: string }>
  override_help: Record<string, string>
}

export interface FeasibilityResponse {
  mission_id: string
  feasible: boolean
  option_count: number
  best_option: Assignment | null
  blocking_reason_counts: Record<string, number>
  candidates_examined: number
  explanations: string[]
  notes: string[]
  reason_codes: Array<{ code: string; label: string }>
}

export interface ReadinessResponse {
  assets: AssetRow[]
  readiness_method: string
  readiness_note: string
  synthetic_data_notice: string
}

export interface ClockResponse {
  now_minute: number
  now_iso: string
  frozen_mission_ids: string[]
  note: string
}

export interface ReasonCodesResponse {
  reason_codes: Array<{ code: string; label: string }>
  risk_formula: string
  explanation_policy: string
}

export interface LiveMessage {
  kind: string
  occurred_at: string
  synthetic_data_notice: string
  advisory_notice: string
  payload: Record<string, unknown>
}