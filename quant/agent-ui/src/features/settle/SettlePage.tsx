import { useAgentStore } from '../../shared/lib/store'
import { Stamp, LotCode } from '../../shared/ui/Stamp'

export function SettlePage() {
  const predictions = useAgentStore((s) => s.predictions)
  const pending = predictions.filter((p) => p.status === 'pending' || p.status === 'shadow')
  const resolved = predictions.filter((p) => p.status === 'resolved')

  return (
    <div>
      <div className="page-head">
        <h1>结算台</h1>
        <p>盘后 Track：到期预测 → hit/miss/PIC。数字由确定性结算代码写入账本。</p>
      </div>
      <section className="zone">
        <div className="zone-head">
          <h2>待结算 / Shadow 在途</h2>
          <span className="count">{pending.length}</span>
        </div>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>pred_id</th>
                <th>对象</th>
                <th>到期</th>
                <th>状态</th>
                <th>放行门</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((p) => (
                <tr key={p.pred_id}>
                  <td>
                    <LotCode>{p.pred_id}</LotCode>
                  </td>
                  <td>{p.object_name || p.object}</td>
                  <td>{p.resolve_at}</td>
                  <td>{p.status}</td>
                  <td>
                    <Stamp gate={p.release_gate} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="zone">
        <div className="zone-head">
          <h2>已结算</h2>
          <span className="count">{resolved.length}</span>
        </div>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>pred_id</th>
                <th>对象</th>
                <th>outcome</th>
                <th>结算日</th>
              </tr>
            </thead>
            <tbody>
              {resolved.map((p) => (
                <tr key={p.pred_id}>
                  <td>
                    <LotCode>{p.pred_id}</LotCode>
                  </td>
                  <td>{p.object_name || p.object}</td>
                  <td>
                    {p.outcome?.hit === true ? (
                      <span className="stamp ok">HIT</span>
                    ) : p.outcome?.hit === false ? (
                      <span className="stamp quarantine">MISS</span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>{p.resolve_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
