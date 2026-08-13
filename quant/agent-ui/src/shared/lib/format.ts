export function pct(x: number | null | undefined, digits = 1): string {
  if (x == null || Number.isNaN(x)) return '—'
  return `${(x * 100).toFixed(digits)}%`
}

export function claimLabel(claim: Record<string, unknown>, claimType: string): string {
  if (claimType === 'direction') {
    const d = claim.direction === 'up' ? '看涨' : claim.direction === 'down' ? '看跌' : String(claim.direction)
    return `${d} vs ${claim.vs ?? '—'}`
  }
  if (claimType === 'interval') {
    const lo = claim.low as number
    const hi = claim.high as number
    return `[${pct(lo)}, ${pct(hi)}] vs ${claim.vs ?? '—'}`
  }
  if (claimType === 'target') {
    return `目标年化 ${pct(claim.target_ann_return as number)} · 纸面约束`
  }
  return JSON.stringify(claim)
}

export const GATE_LABEL: Record<string, string> = {
  released: '放行',
  hold: '暂扣',
  quarantine: '隔离',
  observe: '观察',
}
