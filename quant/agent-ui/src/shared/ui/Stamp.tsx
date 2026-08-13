import { GATE_LABEL } from '../lib/format'
import type { ReleaseGate } from '../api/types'

export function Stamp({ gate }: { gate: ReleaseGate | string }) {
  const g = gate as ReleaseGate
  return <span className={`stamp ${g}`}>{GATE_LABEL[g] ?? g}</span>
}

export function LotCode({ children }: { children: string }) {
  return <span className="lot-code">{children}</span>
}

export function CiPill({
  n,
  rate,
  lo,
  hi,
  label,
}: {
  n: number
  rate: number | null
  lo: number | null
  hi: number | null
  label: string
}) {
  const rateTxt =
    rate == null ? '—' : `${(rate * 100).toFixed(1)}%`
  const ci =
    lo != null && hi != null
      ? `CI ${(lo * 100).toFixed(0)}–${(hi * 100).toFixed(0)}%`
      : 'CI —'
  return (
    <span className="ci-pill" title={label}>
      n={n} · {rateTxt} · {ci}
    </span>
  )
}
