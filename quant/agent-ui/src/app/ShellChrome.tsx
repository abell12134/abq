import { NavLink } from 'react-router-dom'
import { useAgentStore } from '../shared/lib/store'
import { LotCode } from '../shared/ui/Stamp'

const navGroups = [
  {
    label: '账本',
    links: [
      { to: '/', label: '今日放行', end: true },
      { to: '/ledger', label: '预测账本' },
      { to: '/settle', label: '结算台' },
    ],
  },
  {
    label: '信任',
    links: [
      { to: '/scorecard', label: '成绩校准' },
      { to: '/strategies', label: '策略信任' },
    ],
  },
  {
    label: '旁路',
    links: [
      { to: '/research', label: '研究旁路' },
      { to: '/system', label: '系统' },
    ],
  },
]

export function StatusRail() {
  const status = useAgentStore((s) => s.status)
  return (
    <header className="status-rail">
      <span className="brand">Quant Agent</span>
      {status ? (
        <>
          <div className="rail-meta">
            <span>数据日 {status.data_day}</span>
            <LotCode>{status.settlement_caliber}</LotCode>
            <span className={`stamp ${status.mode === 'shadow' ? 'hold' : 'released'}`}>
              {status.mode === 'shadow' ? '冷启动 SHADOW' : '已毕业'}
            </span>
            {status.synthetic_demo ? (
              <span className="stamp quarantine">SYNTHETIC</span>
            ) : null}
          </div>
          <span className="spacer" />
          <span className="disclaimer" title={status.disclaimer}>
            {status.disclaimer}
          </span>
        </>
      ) : (
        <span className="lot">连接账本…</span>
      )}
    </header>
  )
}

export function SideNav() {
  const setOpen = useAgentStore((s) => s.setSupervisorOpen)
  return (
    <nav className="side-nav" aria-label="主导航">
      {navGroups.map((group) => (
        <div key={group.label} className="nav-group">
          <p className="nav-group-label">{group.label}</p>
          {group.links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) => (isActive ? 'active' : '')}
            >
              {l.label}
            </NavLink>
          ))}
        </div>
      ))}
      <div className="nav-foot">
        <button type="button" className="btn primary" onClick={() => setOpen(true)}>
          Supervisor
        </button>
      </div>
    </nav>
  )
}
