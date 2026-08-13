export type ClaimType = 'direction' | 'interval' | 'target'
export type PredStatus = 'pending' | 'resolved' | 'expired' | 'shadow'
export type ReleaseGate = 'released' | 'hold' | 'quarantine' | 'observe'
export type StrategyState = 'champion' | 'challenger' | 'paused' | 'shadow'

export interface Scorecard {
  claim_type: ClaimType
  n: number
  hit_rate: number | null
  pic: number | null
  wilson_low: number | null
  wilson_high: number | null
  sample_ok: boolean
  label: string
}

export interface FeatureSnapshotRef {
  feature_version: string
  pit_timestamp: string
  content_hash: string
  snapshot_ref: string
}

export interface Prediction {
  pred_id: string
  level: 'L1' | 'L2'
  object: string
  object_name: string
  claim_type: ClaimType
  claim: Record<string, unknown>
  horizon: number
  benchmark: string
  settlement_caliber: string
  confidence: number
  raw_confidence: number
  strategy_version: string
  feature_snapshot: FeatureSnapshotRef
  created_at: string
  resolve_at: string
  status: PredStatus
  outcome: Record<string, unknown> | null
  scorecard: Scorecard
  release_gate: ReleaseGate
  failure_conditions: string[]
  critic_notes: string[]
  explanation: string | null
  blend_score?: number | null
  blend_contributors?: { strategy_id: string; weight: number; score: number }[]
}

export interface StrategyTrust {
  strategy_id: string
  name: string
  version: string
  state: StrategyState
  trust_weight: number
  rolling_n: number
  rolling_hit_rate: number | null
  wilson_low: number | null
  pause_reason: string | null
  claim_type: ClaimType
}

export interface SystemStatus {
  data_day: string
  settlement_caliber: string
  mode: 'shadow' | 'graduated'
  shadow_days_remaining: number | null
  released_count: number
  hold_count: number
  quarantine_count: number
  pending_settle_count: number
  synthetic_demo: boolean
  disclaimer: string
}

export interface LlmRouteInfo {
  peak_only?: boolean
  peak_configured?: boolean
  peak_base_url?: string | null
  peak_model?: string | null
  peak_backend?: string | null
  is_peak_hour?: boolean
  error?: string
}

export interface HealthInfo {
  ok: boolean
  service: string
  ledger: 'live' | 'demo' | string
  caliber?: string | null
  langgraph?: boolean
  auth?: {
    ip_whitelist?: boolean
    bearer_token_configured?: boolean
    token_strict?: boolean
  }
  llm?: LlmRouteInfo
}

export interface CalibrationBucket {
  claim_type: ClaimType
  bin_lo: number
  bin_hi: number
  mean_confidence: number
  empirical_rate: number
  n: number
}

export interface SupervisorMessage {
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  pred_id: string | null
  tool_name: string | null
  ts: string
}

export interface SupervisorSession {
  session_id: string
  intent: 'single' | 'portfolio' | 'strategy' | 'general' | null
  messages: SupervisorMessage[]
  attached_pred_ids: string[]
}

export interface ChallengerGateRow {
  strategy_id: string
  n: number
  hits: number
  hit_rate: number | null
  p_value: number
  holm_reject: boolean
  pass_gate: boolean
  reason: string
  oos_rank_ic: number | null
  champion_hit_rate: number | null
}

export interface ResearchQueue {
  sync: { ok: boolean; synced?: number; error?: string; items?: unknown[] }
  gate: {
    ok?: boolean
    challengers: ChallengerGateRow[]
    eligible_to_promote?: string[]
    champion_hit_rate?: number | null
    error?: string
  }
  strategies: StrategyTrust[]
}
