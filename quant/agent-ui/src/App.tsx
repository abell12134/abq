import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './app/AppShell'
import { ReleaseBoard } from './features/release/ReleaseBoard'
import { LedgerPage } from './features/ledger/LedgerPage'
import { SettlePage } from './features/settle/SettlePage'
import { ScorecardPage } from './features/scorecard/ScorecardPage'
import { StrategiesPage } from './features/strategies/StrategiesPage'
import { ResearchPage, SystemPage } from './features/research/ResearchSystem'
import { useAgentStore } from './shared/lib/store'

function ReleaseRoute() {
  const predictions = useAgentStore((s) => s.predictions)
  return <ReleaseBoard predictions={predictions} />
}

export default function App() {
  return (
    <BrowserRouter basename="/agent">
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<ReleaseRoute />} />
          <Route path="ledger" element={<LedgerPage />} />
          <Route path="settle" element={<SettlePage />} />
          <Route path="scorecard" element={<ScorecardPage />} />
          <Route path="strategies" element={<StrategiesPage />} />
          <Route path="research" element={<ResearchPage />} />
          <Route path="system" element={<SystemPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
