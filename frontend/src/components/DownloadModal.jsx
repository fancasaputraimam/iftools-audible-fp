import React, { useState } from 'react'
import { Download } from 'lucide-react'
import { Badge, Button, Dialog } from './ui.jsx'

const TOKEN_KEY = 'iftools_token'

/**
 * Download modal with granular result buckets.
 *
 * options: [{ id, label, hint, count, kind }]
 * cols:    [{ id, label, hint, kinds: [kind, ...] }]
 *
 * Every bucket maps to /api/<prefix>/results?kind=<kind>&meta=<0|1>.
 * Combinations fetch each listed kind and merge client-side, keeping the
 * latest line per email (same rule as the server).
 *
 * meta=0 → plain email:password per line (default)
 * meta=1 → keep the trailing detail (label / name / country / found count)
 */
export default function DownloadModal({ open, prefix, options, cols, onClose, showToast }) {
  const [keepMeta, setKeepMeta] = useState(false)

  async function fetchKind(kind) {
    const token = localStorage.getItem(TOKEN_KEY) || ''
    const url = `/api/${prefix}/results?kind=${encodeURIComponent(kind)}&meta=${keepMeta ? 1 : 0}`
    const res = await fetch(url, { headers: { 'x-access-key': token } })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return (await res.text()).split('\n').filter((l) => l.trim())
  }

  async function download(kinds, name) {
    if (!kinds?.length) return
    try {
      const merged = {}
      for (const k of kinds) {
        for (const ln of await fetchKind(k)) {
          merged[ln.split(':')[0]] = ln
        }
      }
      const lines = Object.values(merged)
      if (!lines.length) {
        showToast?.('Nothing to download for that bucket')
        return
      }
      const blob = new Blob([`${lines.join('\n')}\n`], { type: 'text/plain' })
      const u = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = u
      a.download = name
      a.click()
      URL.revokeObjectURL(u)
      showToast?.(`Downloaded ${lines.length} lines → ${name}`)
    } catch (e) {
      showToast?.(`Download failed: ${e.message}`)
    }
  }

  function buildName(id) {
    return `${prefix}_${keepMeta ? `${id}_full` : id}.txt`
  }

  return (
    <Dialog open={open} onClose={onClose} title="Download results"
      footer={<>
        <label className="dl-meta-toggle">
          <input type="checkbox" checked={keepMeta}
            onChange={(e) => setKeepMeta(e.target.checked)} />
          <span>Keep details <span className="dl-meta-hint">(name, country, count)</span></span>
        </label>
        <Button onClick={onClose}>Close</Button>
      </>}>
      <p className="dl-intro">
        Pick a bucket. Files are written as plain <code>email:password</code> —
        one per line, latest result per account, de-duplicated.
      </p>

      {options?.length > 0 && (
        <div className="dl-group">
          <div className="dl-group-label">Single buckets</div>
          <div className="dl-grid">
            {options.map((o) => (
              <button key={o.id} type="button" className="dl-card"
                onClick={() => download([o.kind], buildName(o.id))}>
                <div className="dl-card-head">
                  <span className="dl-card-label">{o.label}</span>
                  <Badge tone={o.tone || 'muted'}>{o.count}</Badge>
                </div>
                {o.hint && <div className="dl-card-hint">{o.hint}</div>}
              </button>
            ))}
          </div>
        </div>
      )}

      {cols?.length > 0 && (
        <div className="dl-group">
          <div className="dl-group-label">Combinations</div>
          <div className="dl-cols">
            {cols.map((c) => (
              <button key={c.id} type="button" className="dl-col"
                onClick={() => download(c.kinds, buildName(c.id))}>
                <span className="dl-col-label">{c.label}</span>
                {c.hint && <span className="dl-col-hint">{c.hint}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
    </Dialog>
  )
}
