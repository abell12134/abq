import type {
  CalibrationBucket,
  HealthInfo,
  Prediction,
  ResearchQueue,
  StrategyTrust,
  SupervisorSession,
  SystemStatus,
} from './types'

/** BASE_URL is /agent/ in prod → /agent/api; bare / → /api */
function apiRoot(): string {
  const base = import.meta.env.BASE_URL || '/'
  if (base === '/' || base === '') return '/api'
  return `${base.replace(/\/$/, '')}/api`
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${apiRoot()}${path}`)
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${apiRoot()}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => get<HealthInfo>('/health'),
  status: () => get<SystemStatus>('/system/status'),
  predictions: (q?: { gate?: string; status?: string }) => {
    const params = new URLSearchParams()
    if (q?.gate) params.set('gate', q.gate)
    if (q?.status) params.set('status', q.status)
    const s = params.toString()
    return get<Prediction[]>(`/predictions${s ? `?${s}` : ''}`)
  },
  prediction: (id: string) => get<Prediction>(`/predictions/${id}`),
  strategies: () => get<StrategyTrust[]>('/strategies'),
  calibration: () => get<CalibrationBucket[]>('/calibration'),
  researchQueue: () => get<ResearchQueue>('/research/queue'),
  researchSync: () => post<ResearchQueue['sync']>('/research/sync'),
  researchEvaluate: () => post<ResearchQueue['gate']>('/research/evaluate'),
  researchPromote: (strategyId: string, force = false) =>
    post<unknown>(`/research/promote/${encodeURIComponent(strategyId)}?force=${force}`),
  supervisorAsk: (body: {
    session_id?: string | null
    message: string
    pred_id?: string | null
    intent?: string | null
  }) => post<SupervisorSession>('/supervisor/ask', body),
}
