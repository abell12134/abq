import type { Prediction } from '../../shared/api/types'
import { claimLabel, pct } from '../../shared/lib/format'
import { CiPill, LotCode, Stamp } from '../../shared/ui/Stamp'
import { useAgentStore } from '../../shared/lib/store'

export function AssayCard({ pred }: { pred: Prediction }) {
  const selected = useAgentStore((s) => s.selectedPredId === pred.pred_id)
  const selectPred = useAgentStore((s) => s.selectPred)
  const sc = pred.scorecard
  const rate = sc.hit_rate ?? sc.pic

  return (
    <article
      className={`assay${selected ? ' selected' : ''}`}
      onClick={() => selectPred(pred.pred_id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          selectPred(pred.pred_id)
        }
      }}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
    >
      <div>
        <div className="assay-title">
          <span className="name">{pred.object_name || pred.object}</span>
          <LotCode>{pred.object}</LotCode>
          <Stamp gate={pred.release_gate} />
          <span className="lot-code">{pred.level}</span>
        </div>
        <div className="assay-meta">
          <span>
            Claim <strong>{claimLabel(pred.claim, pred.claim_type)}</strong>
          </span>
          <span>
            Horizon <strong>{pred.horizon} 交易日</strong>
          </span>
          <span>
            置信度 <strong>{pct(pred.confidence, 0)}</strong>
            <span style={{ color: 'var(--muted)' }}>（raw {pct(pred.raw_confidence, 0)}）</span>
          </span>
          {pred.blend_score != null ? (
            <span>
              混权分 <strong>{pred.blend_score.toFixed(3)}</strong>
              {pred.blend_contributors && pred.blend_contributors.length > 1 ? (
                <span style={{ color: 'var(--muted)' }}>
                  （{pred.blend_contributors.length} 源）
                </span>
              ) : null}
            </span>
          ) : null}
          <span>
            到期 <strong>{pred.resolve_at}</strong>
          </span>
          <LotCode>{pred.strategy_version}</LotCode>
        </div>
        {pred.explanation ? (
          <p style={{ margin: '10px 0 0', color: 'var(--muted)', fontSize: 13, lineHeight: 1.5 }}>
            {pred.explanation}
          </p>
        ) : null}
      </div>
      <div className="assay-side">
        <CiPill
          n={sc.n}
          rate={rate}
          lo={sc.wilson_low}
          hi={sc.wilson_high}
          label={sc.label}
        />
        <span className="lot-code">{sc.label}</span>
      </div>
    </article>
  )
}

function Zone({
  title,
  items,
  hint,
}: {
  title: string
  items: Prediction[]
  hint: string
}) {
  return (
    <section className="zone">
      <div className="zone-head">
        <h2>{title}</h2>
        <span className="count">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="empty-zone">{hint}</div>
      ) : (
        <div className="assay-list">
          {items.map((p) => (
            <AssayCard key={p.pred_id} pred={p} />
          ))}
        </div>
      )}
    </section>
  )
}

export function ReleaseBoard({ predictions }: { predictions: Prediction[] }) {
  const status = useAgentStore((s) => s.status)
  const released = predictions.filter((p) => p.release_gate === 'released')
  const hold = predictions.filter((p) => p.release_gate === 'hold')
  const quarantine = predictions.filter((p) => p.release_gate === 'quarantine')

  return (
    <div>
      <div className="page-head">
        <h1>今日放行</h1>
        <p>
          主推荐仅含已毕业且样本量达标的批号。Shadow / 样本不足不得进放行区。点选条目可附着到
          Supervisor。
        </p>
        <div className="board-summary" aria-label="分区计数">
          <span className="board-stat released">
            <strong>{released.length}</strong> Released
          </span>
          <span className="board-stat hold">
            <strong>{hold.length}</strong> Hold
          </span>
          <span className="board-stat quarantine">
            <strong>{quarantine.length}</strong> Quarantine
          </span>
        </div>
      </div>
      {status?.mode === 'shadow' ? (
        <div className="watermark">
          系统冷启动中 · 预测仅供观察
          {status.shadow_days_remaining != null
            ? ` · 约剩 ${status.shadow_days_remaining} 个交易日毕业窗口`
            : ''}
        </div>
      ) : null}
      <Zone
        title="Released · 主推荐"
        items={released}
        hint="当前无放行批号。冷启动或策略未毕业时，此区应为空——这是正确行为，不是故障。"
      />
      <Zone
        title="Hold · 研发暂扣 / Shadow"
        items={hold}
        hint="无暂扣样本。"
      />
      <Zone
        title="Quarantine · 隔离（样本不足或暂停）"
        items={quarantine}
        hint="无隔离样本。"
      />
    </div>
  )
}
