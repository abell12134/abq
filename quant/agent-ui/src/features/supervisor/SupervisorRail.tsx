import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { api } from '../../shared/api/client'
import { useAgentStore } from '../../shared/lib/store'
import { BriefMarkdown } from '../../shared/ui/BriefMarkdown'

const QUICK = [
  { label: '成绩与失效', msg: '诊断这条预测的成绩单、样本量与失效条件' },
  { label: '系统信任', msg: '当前 champion 信任权重与 Wilson 区间如何？' },
  { label: '今日放行', msg: '今日放行概况与样本是否足够进主推荐？' },
]

export function SupervisorRail() {
  const {
    selectedPredId,
    session,
    setSession,
    supervisorOpen,
    setSupervisorOpen,
  } = useAgentStore()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [showTrace, setShowTrace] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSupervisorOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setSupervisorOpen])

  useEffect(() => {
    if (!busy) {
      setElapsed(0)
      return
    }
    const t0 = Date.now()
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 500)
    return () => window.clearInterval(id)
  }, [busy])

  const scrollToEnd = useEffectEvent(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  })

  useEffect(() => {
    scrollToEnd()
  }, [session?.messages?.length, busy])

  async function ask(message: string) {
    const msg = message.trim()
    if (!msg || busy) return
    setBusy(true)
    setErr(null)
    try {
      const next = await api.supervisorAsk({
        session_id: session?.session_id,
        message: msg,
        pred_id: selectedPredId,
        intent: selectedPredId ? 'single' : 'general',
      })
      setSession(next)
      setText('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const visible = (session?.messages ?? []).filter(
    (m) => m.role === 'user' || m.role === 'assistant',
  )
  const traces = (session?.messages ?? []).filter((m) => m.role === 'tool')

  return (
    <aside
      className={`supervisor-rail${supervisorOpen ? ' open-mobile' : ''}`}
      aria-label="Supervisor"
    >
      <div className="sup-head">
        <div className="sup-head-row">
          <h2>Supervisor</h2>
          {busy ? <span className="sup-live">编排中 · {elapsed}s</span> : null}
        </div>
        <p>编排与解释 · 数字只读自账本 · 不产生 claim</p>
      </div>

      <div className="sup-messages" aria-live="polite">
        {visible.length === 0 && !busy ? (
          <div className="empty-zone sup-empty">
            <p>选择一条预测后提问，或直接问系统状态 / 策略信任。</p>
            <div className="sup-quick">
              {QUICK.map((q) => (
                <button
                  key={q.label}
                  type="button"
                  className="sup-chip"
                  onClick={() => void ask(q.msg)}
                >
                  {q.label}
                </button>
              ))}
            </div>
          </div>
        ) : (
          visible.map((m, i) =>
            m.role === 'user' ? (
              <div key={`${m.ts}-${i}`} className="msg user">
                {m.content}
              </div>
            ) : (
              <article key={`${m.ts}-${i}`} className="msg assistant brief-card">
                <header className="brief-card-head">
                  <span className="brief-role">分析简报</span>
                  <time dateTime={m.ts}>{m.ts.slice(11, 19)}</time>
                </header>
                <BriefMarkdown text={m.content} />
              </article>
            ),
          )
        )}
        {busy ? (
          <div className="msg assistant brief-card busy" aria-busy="true">
            <div className="sup-busy-bar" />
            <p className="brief-p">
              Peak LLM 编排中（工具取数已完成或进行中）。通常 30–120s，最长约 10 分钟。
            </p>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>

      {traces.length > 0 ? (
        <div className="sup-trace">
          <button type="button" className="sup-trace-toggle" onClick={() => setShowTrace((v) => !v)}>
            {showTrace ? '隐藏' : '查看'}工具轨迹 ({traces.length})
          </button>
          {showTrace ? (
            <pre className="sup-trace-body">
              {traces
                .map((t) => {
                  try {
                    return JSON.stringify(JSON.parse(t.content), null, 2)
                  } catch {
                    return t.content
                  }
                })
                .join('\n---\n')}
            </pre>
          ) : null}
        </div>
      ) : null}

      <div className="sup-compose">
        {err ? <div className="error-banner">{err}</div> : null}
        <div className="sup-attach-row">
          {selectedPredId ? (
            <span className="attached chip">{selectedPredId}</span>
          ) : (
            <span className="attached muted">未附着 pred</span>
          )}
          <div className="sup-quick compact">
            {QUICK.slice(0, 2).map((q) => (
              <button
                key={q.label}
                type="button"
                className="sup-chip"
                disabled={busy}
                onClick={() => void ask(q.msg)}
              >
                {q.label}
              </button>
            ))}
          </div>
        </div>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="诊断成绩、失效条件或信任状态…"
          disabled={busy}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              void ask(text)
            }
          }}
        />
        <div className="row">
          <button
            type="button"
            className="btn primary"
            disabled={busy || !text.trim()}
            onClick={() => void ask(text)}
          >
            {busy ? `编排中 ${elapsed}s` : '发送'}
          </button>
          <span className="sup-hint">⌘/Ctrl + Enter</span>
          <button type="button" className="btn" onClick={() => setSupervisorOpen(false)}>
            收起
          </button>
        </div>
      </div>
    </aside>
  )
}
