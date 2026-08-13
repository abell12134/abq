/** Lightweight markdown-ish renderer for Supervisor briefs (no deps). */

import type { ReactNode } from 'react'

function inlineFormat(text: string): ReactNode[] {
  const parts: ReactNode[] = []
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) {
      parts.push(
        <code key={i++} className="brief-code">
          {tok.slice(1, -1)}
        </code>,
      )
    } else {
      parts.push(<strong key={i++}>{tok.slice(2, -2)}</strong>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function renderBlock(block: string, key: number): ReactNode {
  const lines = block.split('\n').filter((l) => l.trim().length > 0)
  if (lines.length === 0) return null

  const listItems = lines.every(
    (l) => /^[-*•]\s+/.test(l.trim()) || /^\d+\.\s+/.test(l.trim()),
  )
  if (listItems) {
    return (
      <ul key={key} className="brief-list">
        {lines.map((l, i) => (
          <li key={i}>
            {inlineFormat(l.trim().replace(/^[-*•]\s+/, '').replace(/^\d+\.\s+/, ''))}
          </li>
        ))}
      </ul>
    )
  }

  return (
    <p key={key} className="brief-p">
      {lines.map((l, i) => (
        <span key={i}>
          {i > 0 ? <br /> : null}
          {inlineFormat(l)}
        </span>
      ))}
    </p>
  )
}

export function BriefMarkdown({ text }: { text: string }) {
  const raw = text.replace(/\r\n/g, '\n').trim()
  if (!raw) return null

  const chunks = raw.split(/(?=^#{1,3}\s+)/m).filter((c) => c.trim())
  const hasHeads = chunks.some((c) => /^#{1,3}\s+/.test(c.trim()))

  if (!hasHeads) {
    return <div className="brief-body">{renderBlock(raw, 0)}</div>
  }

  return (
    <div className="brief-body">
      {chunks.map((chunk, idx) => {
        const trimmed = chunk.trim()
        const m = trimmed.match(/^(#{1,3})\s+(.+?)(?:\n|$)([\s\S]*)$/)
        if (!m) return renderBlock(trimmed, idx)
        const level = m[1].length
        const title = m[2].trim()
        const body = (m[3] || '').trim()
        const open = idx < 2
        return (
          <details key={idx} className={`brief-sec level-${level}`} open={open}>
            <summary>{title}</summary>
            <div className="brief-sec-body">
              {body ? body.split(/\n{2,}/).map((b, j) => renderBlock(b, j)) : null}
            </div>
          </details>
        )
      })}
    </div>
  )
}
