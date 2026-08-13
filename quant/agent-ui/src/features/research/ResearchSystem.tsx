import { useCallback, useEffect, useState } from 'react'
import { api } from '../../shared/api/client'
import type {
  ChallengerGateRow,
  HealthInfo,
  ResearchQueue,
  SystemStatus,
} from '../../shared/api/types'
import { pct } from '../../shared/lib/format'
import { LotCode } from '../../shared/ui/Stamp'

export function ResearchPage() {
  const [queue, setQueue] = useState<ResearchQueue | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(() => {
    setErr(null)
    api
      .researchQueue()
      .then(setQueue)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function sync() {
    setBusy(true)
    setMsg(null)
    try {
      const r = await api.researchSync()
      setMsg(`已同步 ${r.synced ?? 0} 个 factor_lab 条目`)
      load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function evaluate() {
    setBusy(true)
    setMsg(null)
    try {
      const r = await api.researchEvaluate()
      setMsg(
        `检验完成：可晋升 ${(r.eligible_to_promote || []).length} 个；champion 命中率 ${pct(r.champion_hit_rate ?? null)}`,
      )
      load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function promote(id: string) {
    setBusy(true)
    setMsg(null)
    try {
      const r = (await api.researchPromote(id, false)) as { ok?: boolean; error?: string }
      if (r.ok === false) setErr(r.error || '晋升失败')
      else setMsg(`已晋升 ${id}`)
      load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const rows: ChallengerGateRow[] = queue?.gate?.challengers || []

  return (
    <div>
      <div className="page-head">
        <h1>研究旁路</h1>
        <p>
          factor_lab → challenger 登记 → 二项检验 + Holm 多重校正 → 样本外追踪晋升。未过门不进主推荐权重；信号源仍为
          lgbm_planC。
        </p>
      </div>
      <div className="toolbar">
        <button type="button" className="btn" disabled={busy} onClick={() => void sync()}>
          同步 factor_lab
        </button>
        <button type="button" className="btn primary" disabled={busy} onClick={() => void evaluate()}>
          跑多重检验门
        </button>
        <button type="button" className="btn" disabled={busy} onClick={load}>
          刷新
        </button>
      </div>
      {err ? <div className="error-banner">{err}</div> : null}
      {msg ? <div className="watermark" style={{ borderColor: 'var(--stamp-released)', color: 'var(--stamp-released)' }}>{msg}</div> : null}

      <div className="panel-block">
        <h2>晋升队列（Holm 门）</h2>
        {rows.length === 0 ? (
          <div className="empty-zone">
            暂无 challenger。先点「同步 factor_lab」，再「跑多重检验门」。无结算样本时 n=0，门会标样本不足——这是正确行为。
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>strategy</th>
                  <th>n</th>
                  <th>命中率</th>
                  <th>p</th>
                  <th>Holm</th>
                  <th>oos_ic</th>
                  <th>门</th>
                  <th>原因</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.strategy_id}>
                    <td>
                      <LotCode>{r.strategy_id}</LotCode>
                    </td>
                    <td>{r.n}</td>
                    <td>{pct(r.hit_rate)}</td>
                    <td>{r.p_value.toFixed(4)}</td>
                    <td>{r.holm_reject ? 'reject H0' : '—'}</td>
                    <td>{r.oos_rank_ic == null ? '—' : r.oos_rank_ic.toFixed(4)}</td>
                    <td>
                      <span className={`stamp ${r.pass_gate ? 'released' : 'quarantine'}`}>
                        {r.pass_gate ? 'PASS' : 'HOLD'}
                      </span>
                    </td>
                    <td style={{ whiteSpace: 'normal', maxWidth: 280, textAlign: 'left' }}>{r.reason}</td>
                    <td>
                      <button
                        type="button"
                        className="btn primary"
                        disabled={busy || !r.pass_gate}
                        onClick={() => void promote(r.strategy_id)}
                      >
                        晋升
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="panel-block">
        <h2>策略注册表快照</h2>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>id</th>
                <th>状态</th>
                <th>权重</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(queue?.strategies || []).map((s) => (
                <tr key={s.strategy_id}>
                  <td>
                    <LotCode>{s.strategy_id}</LotCode>
                  </td>
                  <td>{s.state}</td>
                  <td>{s.trust_weight.toFixed(2)}</td>
                  <td style={{ whiteSpace: 'normal', maxWidth: 360, textAlign: 'left' }}>
                    {s.pause_reason || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export function SystemPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.status(), api.health()])
      .then(([s, h]) => {
        setStatus(s)
        setHealth(h)
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  const llm = health?.llm

  return (
    <div>
      <div className="page-head">
        <h1>系统</h1>
        <p>口径版本、金标准测试、与运维看板互链。结算口径升级时旧 outcome 与新 caliber 并存。</p>
      </div>
      {err ? <div className="error-banner">{err}</div> : null}

      <div className="panel-block">
        <h2>运行态</h2>
        {status ? (
          <dl className="kv-grid">
            <dt>数据日</dt>
            <dd>{status.data_day}</dd>
            <dt>结算口径</dt>
            <dd>
              <code>{status.settlement_caliber}</code>
            </dd>
            <dt>模式</dt>
            <dd>{status.mode}</dd>
            <dt>账本</dt>
            <dd>{health?.ledger ?? '—'}</dd>
            <dt>活跃口径</dt>
            <dd>
              <code>{health?.caliber || status.settlement_caliber}</code>
            </dd>
            <dt>LangGraph</dt>
            <dd>{health?.langgraph ? '开' : '关'}</dd>
            <dt>鉴权</dt>
            <dd>
              IP白名单={health?.auth?.ip_whitelist ? '是' : '否'} · Bearer=
              {health?.auth?.bearer_token_configured ? '已配置' : '未配置'}
              {health?.auth?.token_strict ? ' · STRICT' : ''}
            </dd>
            <dt>待结算</dt>
            <dd>{status.pending_settle_count}</dd>
          </dl>
        ) : (
          <p style={{ color: 'var(--muted)', margin: 0 }}>加载中…</p>
        )}
      </div>

      <div className="panel-block">
        <h2>LLM · Peak 优先</h2>
        {llm?.error ? (
          <p style={{ color: 'var(--danger, #b33)', margin: 0 }}>{llm.error}</p>
        ) : llm ? (
          <dl className="kv-grid">
            <dt>Peak 已配置</dt>
            <dd>{llm.peak_configured ? '是' : '否'}</dd>
            <dt>仅 Peak</dt>
            <dd>{llm.peak_only ? 'AGENT_LLM_PEAK_ONLY=1' : '可回退 offpeak'}</dd>
            <dt>模型</dt>
            <dd>
              <code>{llm.peak_model || '—'}</code>
            </dd>
            <dt>后端</dt>
            <dd>{llm.peak_backend || '—'}</dd>
            <dt>Base URL</dt>
            <dd>
              <code style={{ wordBreak: 'break-all' }}>{llm.peak_base_url || '—'}</code>
            </dd>
            <dt>当前峰时</dt>
            <dd>{llm.is_peak_hour ? '是（时钟）' : '否（Agent 仍优先 peak）'}</dd>
          </dl>
        ) : (
          <p style={{ color: 'var(--muted)', margin: 0 }}>加载中…</p>
        )}
      </div>

      <div className="panel-block">
        <h2>互链</h2>
        <p style={{ margin: 0 }}>
          公网入口：{' '}
          <a href="http://43.159.136.65:8000/agent/" target="_blank" rel="noreferrer">
            http://43.159.136.65:8000/agent/
          </a>
        </p>
        <p style={{ margin: '8px 0 0', color: 'var(--muted)', fontSize: 13 }}>
          金标准：<code>cd quant && ../quant-venv/bin/python agent/run_tests.py</code>
        </p>
      </div>
      <div className="panel-block">
        <h2>鉴权</h2>
        <p style={{ margin: 0, color: 'var(--muted)' }}>
          与 webapp 共用 IP 白名单（configs/webapp.local.yaml）。
        </p>
      </div>
    </div>
  )
}
