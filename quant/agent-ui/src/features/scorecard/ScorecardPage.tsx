import { useEffect, useState } from 'react'
import { api } from '../../shared/api/client'
import type { CalibrationBucket } from '../../shared/api/types'
import { pct } from '../../shared/lib/format'

function CalPanel({
  title,
  subtitle,
  buckets,
  empty,
}: {
  title: string
  subtitle: string
  buckets: CalibrationBucket[]
  empty: string
}) {
  return (
    <div className="panel-block">
      <h2>{title}</h2>
      <p style={{ marginTop: 0, color: 'var(--muted)', fontSize: 13 }}>{subtitle}</p>
      {buckets.length === 0 ? (
        <p style={{ color: 'var(--muted)', fontSize: 13 }}>{empty}</p>
      ) : (
        <div className="cal-bars">
          {buckets.map((b) => (
            <div key={`${b.claim_type}-${b.bin_lo}-${b.bin_hi}`} className="cal-row">
              <span>
                {pct(b.bin_lo, 0)}–{pct(b.bin_hi, 0)}
              </span>
              <div
                className="cal-track"
                title={`conf ${pct(b.mean_confidence)} vs emp ${pct(b.empirical_rate)}`}
              >
                <span className="conf" style={{ width: `${b.mean_confidence * 100}%` }} />
                <span className="emp" style={{ width: `${b.empirical_rate * 100}%` }} />
              </div>
              <span>n={b.n}</span>
            </div>
          ))}
        </div>
      )}
      <p style={{ marginTop: 12, color: 'var(--muted)', fontSize: 12 }}>
        浅条=平均置信度 · 实条=经验频率。校准目标是两者对齐。
      </p>
    </div>
  )
}

export function ScorecardPage() {
  const [buckets, setBuckets] = useState<CalibrationBucket[]>([])
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api
      .calibration()
      .then(setBuckets)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  const direction = buckets.filter((b) => b.claim_type === 'direction')
  const interval = buckets.filter((b) => b.claim_type === 'interval')

  return (
    <div>
      <div className="page-head">
        <h1>成绩与校准</h1>
        <p>
          按 claim_type 分栏：方向命中率与区间 PIC 永不混算。可靠性图对比名义置信度与经验频率。
        </p>
      </div>
      {err ? <div className="error-banner">{err}</div> : null}
      <CalPanel
        title="方向 · 分桶可靠性"
        subtitle="经验频率 = 方向命中率（excess vs benchmark）"
        buckets={direction}
        empty="暂无已结算方向预测分桶"
      />
      <CalPanel
        title="区间 · PIC 分桶可靠性"
        subtitle="经验频率 = Prediction Interval Coverage（实值落入区间比例）"
        buckets={interval}
        empty="暂无已结算区间预测分桶"
      />
    </div>
  )
}
