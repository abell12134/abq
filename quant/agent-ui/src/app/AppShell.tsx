import { useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { api } from '../shared/api/client'
import { useAgentStore } from '../shared/lib/store'
import { SideNav, StatusRail } from './ShellChrome'
import { SupervisorRail } from '../features/supervisor/SupervisorRail'

export function AppShell() {
  const setStatus = useAgentStore((s) => s.setStatus)
  const setPredictions = useAgentStore((s) => s.setPredictions)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [st, preds] = await Promise.all([api.status(), api.predictions()])
        if (cancelled) return
        setStatus(st)
        setPredictions(preds)
        setErr(null)
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [setStatus, setPredictions])

  return (
    <div className="app-shell">
      <StatusRail />
      <div className="shell-body">
        <SideNav />
        <main className="main-pane">
          {err ? (
            <div className="error-banner">
              无法连接 Agent API（{err}）。请先启动{' '}
              <code className="lot-code">bash quant/agent_api/serve.sh</code>
            </div>
          ) : null}
          {loading ? <div className="loading">装载账本…</div> : <Outlet />}
        </main>
        <SupervisorRail />
      </div>
    </div>
  )
}
