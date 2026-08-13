import { useEffect, useState } from 'react'
import { api } from '../../shared/api/client'
import type { StrategyTrust } from '../../shared/api/types'
import { pct } from '../../shared/lib/format'
import { LotCode } from '../../shared/ui/Stamp'

export function StrategiesPage() {
  const [rows, setRows] = useState<StrategyTrust[]>([])
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api
      .strategies()
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  return (
    <div>
      <div className="page-head">
        <h1>策略信任</h1>
        <p>L3 状态机：champion / challenger / paused。权重由结算滚动窗驱动，非 LLM 主观分。</p>
      </div>
      {err ? <div className="error-banner">{err}</div> : null}
      <div className="panel-block">
        {rows.map((s) => (
          <div key={s.strategy_id} className="trust-row">
            <div>
              <div style={{ fontWeight: 650 }}>{s.name}</div>
              <div className="assay-meta">
                <LotCode>{s.version}</LotCode>
                <span className={`stamp ${s.state === 'paused' ? 'quarantine' : s.state === 'challenger' ? 'hold' : 'released'}`}>
                  {s.state}
                </span>
                <span>n={s.rolling_n}</span>
                <span>命中 {pct(s.rolling_hit_rate)}</span>
                <span>Wilson↓ {pct(s.wilson_low)}</span>
              </div>
              {s.pause_reason ? (
                <div style={{ marginTop: 4, color: 'var(--stamp-quarantine)', fontSize: 12 }}>{s.pause_reason}</div>
              ) : null}
            </div>
            <div className="trust-bar" aria-label="trust weight">
              <span style={{ width: `${Math.min(100, s.trust_weight * 100)}%` }} />
            </div>
            <LotCode>{s.trust_weight.toFixed(2)}</LotCode>
          </div>
        ))}
      </div>
    </div>
  )
}
