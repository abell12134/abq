import { useAgentStore } from '../../shared/lib/store'
import { claimLabel, pct } from '../../shared/lib/format'
import { Stamp, LotCode } from '../../shared/ui/Stamp'

export function LedgerPage() {
  const predictions = useAgentStore((s) => s.predictions)
  const selectPred = useAgentStore((s) => s.selectPred)
  const selected = useAgentStore((s) => s.selectedPredId)

  return (
    <div>
      <div className="page-head">
        <h1>预测账本</h1>
        <p>全量 L1/L2 可结算预测。每条均可追溯 pred_id、口径版本与成绩单。</p>
      </div>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>pred_id</th>
              <th>对象</th>
              <th>层级</th>
              <th>Claim</th>
              <th>状态</th>
              <th>放行</th>
              <th>置信度</th>
              <th>到期</th>
            </tr>
          </thead>
          <tbody>
            {predictions.map((p) => (
              <tr
                key={p.pred_id}
                style={{ cursor: 'pointer', background: selected === p.pred_id ? '#e8eef6' : undefined }}
                onClick={() => selectPred(p.pred_id)}
              >
                <td>
                  <LotCode>{p.pred_id}</LotCode>
                </td>
                <td>
                  {p.object_name || p.object}
                </td>
                <td>{p.level}</td>
                <td>{claimLabel(p.claim, p.claim_type)}</td>
                <td>{p.status}</td>
                <td>
                  <Stamp gate={p.release_gate} />
                </td>
                <td>{pct(p.confidence, 0)}</td>
                <td>{p.resolve_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected ? (
        <Detail predId={selected} />
      ) : (
        <p className="empty-zone" style={{ marginTop: 16 }}>
          点击一行查看特征快照引用、失效条件与 Critic 备注。
        </p>
      )}
    </div>
  )
}

function Detail({ predId }: { predId: string }) {
  const p = useAgentStore((s) => s.predictions.find((x) => x.pred_id === predId))
  if (!p) return null
  const fs = p.feature_snapshot
  return (
    <div className="panel-block" style={{ marginTop: 16 }}>
      <h2>批号详情 · {p.pred_id}</h2>
      <div className="assay-meta">
        <span>
          策略 <LotCode>{p.strategy_version}</LotCode>
        </span>
        <span>
          口径 <LotCode>{p.settlement_caliber}</LotCode>
        </span>
        <span>
          特征 {fs.feature_version} · PIT {fs.pit_timestamp}
        </span>
        <span>
          hash <LotCode>{fs.content_hash}</LotCode>
        </span>
        <span>
          snapshot <LotCode>{fs.snapshot_ref}</LotCode>
        </span>
      </div>
      <h2 style={{ marginTop: 14 }}>失效条件</h2>
      <ul>
        {p.failure_conditions.map((f) => (
          <li key={f}>{f}</li>
        ))}
      </ul>
      <h2>Critic</h2>
      <ul>
        {p.critic_notes.map((f) => (
          <li key={f}>{f}</li>
        ))}
      </ul>
      {p.outcome ? (
        <>
          <h2>结算 outcome</h2>
          <pre style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{JSON.stringify(p.outcome, null, 2)}</pre>
        </>
      ) : null}
    </div>
  )
}
