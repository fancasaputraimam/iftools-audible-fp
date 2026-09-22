import React, { useEffect, useRef, useState } from 'react'
import {
  AudioLines,
  CheckCircle2,
  Clock3,
  Copy,
  Download,
  FileText,
  Gauge,
  ListChecks,
  ListEnd,
  Pause,
  Play,
  Radio,
  ScrollText,
  Square,
  Target,
  Trash2,
  Upload,
  XCircle,
  FolderOpen,
} from 'lucide-react'
import { api, subscribeLogs } from '../api.js'
import { Badge, Button, Card, Dialog, Input, Spinner } from './ui.jsx'

/* ---------------------------------------------------------------
   Audible FP Checker — same layout language as StatusPanel
   (GitHub Register): hero → metrics → progress → controls.
   --------------------------------------------------------------- */

const SPEEDS = [
  { id: 'slow', label: 'Slow' },
  { id: 'normal', label: 'Normal' },
  { id: 'fast', label: 'Fast' },
  { id: 'maximum', label: 'Maximum' },
]

const fmtTime = (ts) =>
  ts ? new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: false }) : '-'

const fmtDuration = (sec) => {
  if (!Number.isFinite(sec) || sec < 0) return '-'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${pad(h)}.${pad(m)}.${pad(r)}` : `${pad(m)}.${pad(r)}`
}

function StatusChip({ s, note }) {
  if (s === 'ok') {
    // Show specific secondary type when available
    if (note) {
      const n = note.toLowerCase()
      if (n.startsWith('v2l')) return <Badge tone="success">v2l ✓</Badge>
      if (n.startsWith('dcq:zip')) return <Badge tone="success">DCQ ZIP</Badge>
      if (n.startsWith('dcq:name')) return <Badge tone="success">DCQ Name</Badge>
      if (n.startsWith('dcq:phone')) return <Badge tone="success">DCQ Phone</Badge>
      if (n.startsWith('dcq:')) return <Badge tone="success">DCQ</Badge>
      if (n.startsWith('cc:')) return <Badge tone="success">CC Expiry</Badge>
      if (n.startsWith('push_notif')) return <Badge tone="success">Push</Badge>
      if (n.startsWith('otp_sms') || n.startsWith('otp_email')) return <Badge tone="success">OTP ✓</Badge>
      if (n.startsWith('otp_wa')) return <Badge tone="success">OTP WA ✓</Badge>
    }
    return <Badge tone="success">Hit</Badge>
  }
  if (s === 'fail' || s === 'error') {
    if (note) {
      const n = note.toLowerCase()
      if (n.includes('no_otp')) return <Badge tone="warning">No OTP</Badge>
      if (n.includes('not_amazon')) return <Badge tone="muted">Not Amazon</Badge>
      if (n.includes('amazon_error')) return <Badge tone="warning">Amz Err</Badge>
      if (n.includes('no_forgot')) return <Badge tone="muted">No URL</Badge>
    }
    return <Badge tone="danger">Bad</Badge>
  }
  // status=check = secondary verification type shown in note
  if (note) {
    const n = note.toLowerCase()
    if (n.startsWith('v2l')) return <Badge tone="success">v2l ✓</Badge>
    if (n.startsWith('dcq')) return <Badge tone="info">DCQ</Badge>
    if (n.startsWith('cc:')) return <Badge tone="warning">CC</Badge>
    if (n.startsWith('push_notif')) return <Badge tone="muted">Push</Badge>
    if (n.startsWith('otp_sms')) return <Badge tone="muted">OTP SMS</Badge>
  }
  return <Badge tone="warning">Check</Badge>
}

function countParsed(text) {
  return text
    .split('\n')
    .filter((l) => {
      const t = l.trim()
      return t.length > 0 && !t.startsWith('#')
    })
    .length
}

export default function AudiblePanel() {
  const [accounts, setAccounts] = useState('') // text blob email:pass
  const [fileName, setFileName] = useState('')
  const [speed, setSpeed] = useState('normal')
  const [proxyFile, setProxyFile] = useState('')
  const [proxyName, setProxyName] = useState('')
  const [proxySample, setProxySample] = useState('')
  const [workers, setWorkers] = useState('')
  const [limit, setLimit] = useState('')
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState([])
  const [hitCount, setHitCount] = useState(0)
  const [failCount, setFailCount] = useState(0)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [stopOpen, setStopOpen] = useState(false)
  const [now, setNow] = useState(Date.now())
  const pollRef = useRef(null)
  const toastTimer = useRef(null)
  const tickTimer = useRef(null)

  // --- streaming log (SSE /api/logs) ---
  const [logLines, setLogLines] = useState([])
  const [logLive, setLogLive] = useState(false)
  const [logFollow, setLogFollow] = useState(true)
  const logBoxRef = useRef(null)
  const logUnsubRef = useRef(() => {})

  useEffect(() => {
    let closed = false
    api
      .get('/api/logs/snapshot?limit=500')
      .then((data) => {
        if (closed) return
        setLogLines(data.lines || [])
        setLogLive(true)
        subscribeLogs(data.seq || 0, (line) => {
          if (closed) return
          setLogLines((prev) => [...prev.slice(-1499), line])
        })
          .then((unsub) => {
            if (!closed) logUnsubRef.current = unsub
          })
          .catch(() => {})
      })
      .catch(() => {})
    return () => {
      closed = true
      logUnsubRef.current()
      setLogLive(false)
    }
  }, [])

  useEffect(() => {
    const el = logBoxRef.current
    if (!logFollow || !el) return
    // Keep the newest event visible while the stream is active.
    el.scrollTop = el.scrollHeight
  }, [logLines, logFollow])

  function logTone(line) {
    return line.includes('[+]')
      ? 'success'
      : line.includes('[-]') || line.includes('[!]')
        ? 'danger'
        : line.includes('[*]')
          ? 'accent'
          : ''
  }

  function showToast(msg) {
    setToast(msg)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(''), 2600)
  }

  // Live clock so the elapsed counter ticks while running.
  useEffect(() => {
    tickTimer.current = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tickTimer.current)
  }, [])

  // Poll job status while running.
  useEffect(() => {
    if (!running) {
      clearInterval(pollRef.current)
      return undefined
    }
    const tick = () => {
      api
        .get('/api/audible/status')
        .then((d) => {
          setStatus(d)
          setResults(d.results || [])
          setHitCount(d.hits || 0)
          setFailCount(d.fails || 0)
          if (!d.running) {
            setRunning(false)
            showToast(d.done ? 'Job complete' : 'Job stopped')
          }
        })
        .catch(() => {})
    }
    tick()
    pollRef.current = setInterval(tick, 1200)
    return () => clearInterval(pollRef.current)
  }, [running])

  // Restore any job still running server-side on mount.
  useEffect(() => {
    api
      .get('/api/audible/status')
      .then((d) => {
        if (d && d.running) {
          setRunning(true)
          setStatus(d)
          setResults(d.results || [])
        } else if (d && Array.isArray(d.results) && d.results.length) {
          setResults(d.results)
          setHitCount(d.hits || 0)
          setFailCount(d.fails || 0)
        }
      })
      .catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  async function onPickAccounts(e) {
    const f = e.target.files?.[0]
    if (!f) return
    const text = await f.text()
    setAccounts(text)
    setFileName(f.name)
    const n = countParsed(text)
    showToast(`${f.name} — ${n} account${n !== 1 ? 's' : ''} parsed`)
  }

  async function onPickProxies(e) {
    const f = e.target.files?.[0]
    if (!f) return
    const text = await f.text()
    setProxyFile(text)
    setProxyName(f.name)
    const n = countParsed(text)
    // sample first non-comment line for format hint
    const sample = text.split('\n').find((l) => l.trim() && !l.startsWith('#'))?.trim() || ''
    showToast(`${f.name} — ${n} proxy${n !== 1 ? 'ies' : ''} loaded`)
    setProxySample(sample)
  }

  async function startJob() {
    if (busy) return
    const list = accounts
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#') && l.includes(':'))
    if (!list.length) {
      showToast('Load an accounts.txt first (email:password per line)')
      return
    }
    if (!proxyFile) {
      showToast('Proxy list is required — datacenter IP is blocked by Audible')
      return
    }
    setBusy(true)
    try {
      await api.post('/api/audible/start', {
        accounts: list,
        speed,
        proxy_file: proxyFile || undefined,
        workers: workers ? Number(workers) : undefined,
        limit: limit ? Number(limit) : undefined,
      })
      setRunning(true)
      showToast(`Job started — ${list.length} accounts · ${speed}`)
    } catch (e) {
      showToast(`Start failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function stopJob() {
    setStopOpen(false)
    try {
      await api.post('/api/audible/stop', {})
      showToast('Stop requested')
    } catch (e) {
      showToast(`Stop failed: ${e.message}`)
    }
  }

  async function download(kind) {
    try {
      const res = await fetch(`/api/audible/results?kind=${kind}`, {
        headers: { 'x-access-key': localStorage.getItem('iftools_token') || '' },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const text = await res.text()
      const blob = new Blob([text], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = kind === 'hits' ? 'audible_hits.txt' : 'audible_results.txt'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      showToast(`Download failed: ${e.message}`)
    }
  }

  async function copyRow(row) {
    try {
      await navigator.clipboard.writeText(row)
      showToast('Copied')
    } catch {
      showToast('Copy failed')
    }
  }

  // ---- derived metrics (same shape as StatusPanel) ----
  const total = results.length
  const target = status?.total || 0
  const pct = target ? Math.min(100, Math.round((total / target) * 100)) : 0
  const done = hitCount + failCount

  // Break down hits by secondary type
  const v2lCount = results.filter((r) => r.note?.toLowerCase().startsWith('v2l')).length
  const dcqCount = results.filter((r) => r.note?.toLowerCase().startsWith('dcq')).length
  const otpCount = results.filter((r) => {
    const n = r.note?.toLowerCase() || ''
    return (n.startsWith('otp_sms') || n.startsWith('otp_email')) && r.status === 'ok'
  }).length

  const elapsedSec = (() => {
    if (!status?.started_at) return 0
    const end = status?.finished_at ? status.finished_at * 1000 : now
    return Math.max(0, (end - status.started_at * 1000) / 1000)
  })()

  const statusInfo = (() => {
    if (running) return { label: 'Running', tone: 'ok', title: 'Validating accounts' }
    if (status?.error) return { label: 'Error', tone: 'bad', title: 'Job failed' }
    if (status?.finished_at) {
      if (target > 0 && total >= target && failCount === 0)
        return { label: 'Completed', tone: 'ok', title: 'All accounts checked' }
      if (target > 0 && total < target)
        return { label: 'Stopped', tone: 'muted', title: 'Job stopped before completion' }
      if (failCount > 0 && hitCount === 0)
        return { label: 'All failed', tone: 'bad', title: 'No hits at all' }
      return { label: 'Completed', tone: 'ok', title: 'Job completed' }
    }
    return { label: 'Ready', tone: 'muted', title: 'Ready to check' }
  })()

  return (
    <div style={styles.wrap}>
      {/* hero */}
      <Card style={styles.hero}>
        <div style={styles.heroText}>
          <div style={styles.eyebrow}>AUDIBLE FP CHECKER</div>
          <h1 style={styles.heroTitle}>{statusInfo.title}</h1>
          <p style={styles.heroSub}>
            Audible.de forgot-password validation. Fingerprint stealth, IMAP OTP
            retrieval and proxy rotation run server-side. A proxy list is
            required — the datacenter IP is blocked by Audible.
          </p>
        </div>
        <Badge
          tone={
            statusInfo.tone === 'ok'
              ? 'success'
              : statusInfo.tone === 'bad'
                ? 'danger'
                : 'muted'
          }
          className="status-hero-badge"
        >
          {running && <span className="pulse-dot" />}
          {statusInfo.label}
        </Badge>
      </Card>

      {/* Metrics share one surface with hairline dividers. */}
      <Card style={styles.metricsCard}>
        <div className="status-metrics">
          <Stat label="Target" value={target || '-'} icon={Target} />
          <Stat label="Hits" value={hitCount} tone="ok" icon={CheckCircle2} />
          <Stat label="Fails" value={failCount} tone="bad" icon={XCircle} />
          <Stat
            label="Progress"
            value={target > 0 ? `${total}/${target}` : '-'}
            hint={target > 0 ? `${pct}%` : null}
            icon={ListChecks}
          />
          <Stat label="Started" value={fmtTime(status?.started_at)} icon={Play} small />
          <Stat
            label={running ? 'Elapsed' : 'Duration'}
            value={fmtDuration(elapsedSec)}
            icon={running ? Clock3 : Square}
            small
          />
        </div>
        {hitCount > 0 && (
          <div style={styles.hitBreakdown}>
            {v2lCount > 0 && <Badge tone="success">v2l: {v2lCount}</Badge>}
            {dcqCount > 0 && <Badge tone="info">DCQ: {dcqCount}</Badge>}
            {otpCount > 0 && <Badge tone="muted">OTP SMS: {otpCount}</Badge>}
            {hitCount - v2lCount - dcqCount - otpCount > 0 && (
              <Badge tone="muted">Other: {hitCount - v2lCount - dcqCount - otpCount}</Badge>
            )}
          </div>
        )}
      </Card>

      {/* Progress */}
      {target > 0 && (
        <Card style={styles.progressCard}>
          <div style={styles.progressHead}>
            <span style={{ color: 'var(--text-3)', fontWeight: 600 }}>Progress</span>
            <span style={{ fontWeight: 700 }}>
              {total} / {target}{' '}
              <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>({pct}%)</span>
            </span>
          </div>
          <div style={styles.progressTrack}>
            <div
              role="progressbar"
              aria-valuenow={pct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Checker progress"
              style={{
                ...styles.progressFill,
                width: `${pct}%`,
                background:
                  statusInfo.tone === 'bad'
                    ? 'var(--danger)'
                    : !running && statusInfo.tone === 'muted'
                      ? 'var(--border-strong)'
                      : 'var(--sage)',
              }}
            />
          </div>
          {(hitCount > 0 || failCount > 0) && (
            <div style={styles.progressLegend}>
              <span style={{ color: 'var(--success-deep)' }}>● {hitCount} hits</span>
              <span style={{ color: 'var(--danger-deep)' }}>● {failCount} fails</span>
              {running && target - total > 0 && (
                <span style={{ color: 'var(--text-3)' }}>● {target - total} pending</span>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Controls: step-based layout — ① Upload → ② Configure → ③ Start */}
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div className="ui-card-strip">
          <ListChecks size={15} />
          <strong>Configure Run</strong>
        </div>
        <div className="run-steps">

          {/* Step 1: Files */}
          <div className="run-step">
            <div className="run-step-num">1</div>
            <div className="run-step-body">
              <div className="run-step-title">Upload files</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">
                    Accounts <span className="run-field-required">required</span>
                  </div>
                  <label className="run-file-btn">
                    <FolderOpen size={14} />
                    <span>{fileName || 'Choose accounts.txt'}</span>
                    <input type="file" accept=".txt" onChange={onPickAccounts} hidden />
                  </label>
                  {accounts ? (
                    <div className="run-field-hint ok">
                      <code>{countParsed(accounts)} accounts · format: email:password</code>
                    </div>
                  ) : (
                    <div className="run-field-hint">One <code>email:password</code> per line</div>
                  )}
                </div>
                <div className="run-field-group">
                  <div className="run-field-label">
                    Proxies <span className="run-field-required">required</span>
                  </div>
                  <label className="run-file-btn">
                    <Upload size={14} />
                    <span>{proxyName || 'Choose proxies.txt'}</span>
                    <input type="file" accept=".txt" onChange={onPickProxies} hidden />
                  </label>
                  {proxyFile ? (
                    <div className="run-field-hint ok">
                      <code>{countParsed(proxyFile)} proxies{proxySample ? ` · ${proxySample.slice(0, 36)}…` : ''}</code>
                    </div>
                  ) : (
                    <div className="run-field-hint">Format: <code>http://user:pass@host:port</code></div>
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className="run-step-divider" />

          {/* Step 2: Speed + workers */}
          <div className="run-step">
            <div className="run-step-num">2</div>
            <div className="run-step-body">
              <div className="run-step-title">Configure</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">Speed</div>
                  <div className="glass-segmented" role="group" aria-label="Speed selection">
                    {SPEEDS.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        className={speed === s.id ? 'active' : ''}
                        aria-pressed={speed === s.id}
                        onClick={() => setSpeed(s.id)}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>
                  <div className="run-field-hint">
                    {speed === 'maximum' ? '⚠ Maximum needs stable proxies' : speed === 'slow' ? 'Best accuracy, ~1 acc/min' : speed === 'normal' ? '~4–6 acc/min, recommended' : '~8–10 acc/min'}
                  </div>
                </div>
                <div className="run-field-group">
                  <label htmlFor="audible-workers" className="run-field-label">
                    Workers
                    <span className="run-field-tooltip" title="Use 1 for maximum accuracy — rotating proxy can assign same IP to parallel workers">ⓘ</span>
                  </label>
                  <Input
                    id="audible-workers"
                    type="number"
                    min="1"
                    max="16"
                    placeholder="Auto"
                    value={workers}
                    onChange={(e) => setWorkers(e.target.value)}
                    className="status-count-input"
                    autoComplete="off"
                    name="audible-workers-x"
                    data-lpignore="true"
                    data-1p-ignore="true"
                  />
                  <div className="run-field-hint">1 = best accuracy</div>
                </div>
                <div className="run-field-group">
                  <label htmlFor="audible-limit" className="run-field-label">
                    Limit
                    <span className="run-field-tooltip" title="Only check the first N accounts and skip the rest">ⓘ</span>
                  </label>
                  <Input
                    id="audible-limit"
                    type="number"
                    min="1"
                    placeholder="All"
                    value={limit}
                    onChange={(e) => setLimit(e.target.value)}
                    className="status-count-input"
                    autoComplete="off"
                    name="audible-limit-x"
                    data-lpignore="true"
                    data-1p-ignore="true"
                  />
                  <div className="run-field-hint">Leave blank for all</div>
                </div>
              </div>
            </div>
          </div>

          <div className="run-step-divider" />

          {/* Step 3: Actions */}
          <div className="run-step">
            <div className="run-step-num">3</div>
            <div className="run-step-body">
              <div className="run-step-title">Run</div>
              <div className="status-actions" style={{ flexWrap: 'wrap' }}>
                {running ? (
                  <Button
                    variant="destructive"
                    className="status-action-stop"
                    onClick={() => setStopOpen(true)}
                  >
                    <Pause size={15} /> Stop
                  </Button>
                ) : (
                  <Button
                    variant="primary"
                    className="status-action-primary"
                    onClick={startJob}
                    disabled={busy || !accounts || !proxyFile}
                    title={!accounts ? 'Load accounts.txt first' : !proxyFile ? 'Load proxies.txt first' : ''}
                  >
                    {busy ? <Spinner /> : <Play size={16} />} Start checker
                  </Button>
                )}
                <Button
                  className="status-action-nav"
                  onClick={() => download('hits')}
                  disabled={!hitCount}
                  title="Download OTP/v2l/DCQ hits only"
                >
                  <Download size={15} /> Hits ({hitCount})
                </Button>
                <Button
                  className="status-action-nav"
                  onClick={() => download('all')}
                  disabled={!total}
                  title="Download all results"
                >
                  <FileText size={15} /> All ({total})
                </Button>
              </div>
            </div>
          </div>

        </div>
      </Card>

      {/* Results */}
      {total > 0 ? (
        <Card className="accounts-head" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="ui-card-strip">
            <Gauge size={16} />
            <strong>Results</strong>
          </div>
          <div className="accounts-table-wrap">
            <table className="accounts-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Email</th>
                  <th>Password</th>
                  <th>Status</th>
                  <th>Note</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={`${r.email}-${i}`}>
                    <td data-label="#">{i + 1}</td>
                    <td data-label="Email">
                      <code style={{ fontSize: 12 }}>{r.email}</code>
                    </td>
                    <td data-label="Password">
                      <button
                        type="button"
                        className="copy-toggle"
                        onClick={() => copyRow(`${r.email}:${r.password}`)}
                        title="Copy email:password"
                      >
                        <code style={{ fontSize: 12 }}>••••••••</code>
                      </button>
                    </td>
                    <td data-label="Status">
                      <StatusChip s={r.status} note={r.note} />
                    </td>
                    <td data-label="Note">
                      <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{r.note || '—'}</span>
                    </td>
                    <td data-label="Actions">
                      <button
                        type="button"
                        className="copy-btn"
                        onClick={() => copyRow(`${r.email}:${r.password}`)}
                        title="Copy"
                        aria-label="Copy credentials"
                      >
                        <Copy size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <Card>
          <div className="ui-empty-state">
            <AudioLines size={28} />
            <strong>No results yet</strong>
            <p>
              Load an accounts.txt, pick a proxy list, then start the checker.
              Results appear here in real time.
            </p>
          </div>
        </Card>
      )}

      <Card className="log-layout" style={{ maxWidth: '100%' }}>
        <div className="log-toolbar">
          <div className="log-toolbar-group">
            <Badge tone={logLive ? 'success' : 'muted'}>
              <Radio size={12} />
              {logLive ? 'Streaming' : 'Connecting'}
            </Badge>
            <span>{logLines.length} lines</span>
          </div>
          <div className="log-toolbar-group">
            <label className="log-follow">
              <input
                type="checkbox"
                checked={logFollow}
                onChange={(e) => setLogFollow(e.target.checked)}
              />
              Auto-scroll
            </label>
            <Button size="sm" onClick={() => setLogLines([])}>
              <Trash2 size={14} /> Clear
            </Button>
          </div>
        </div>
        <div className="log-terminal" ref={logBoxRef}>
          {logLines.length === 0 ? (
            <div className="ui-empty-state">
              <ScrollText size={26} />
              <strong>No logs yet</strong>
              <p>Start a run and every checker step streams here in real time.</p>
            </div>
          ) : (
            logLines.map((line, index) => (
              <div key={index} className={`log-line ${logTone(line)}`}>
                {line}
              </div>
            ))
          )}
        </div>
      </Card>

      <Dialog
        open={stopOpen}
        onClose={() => setStopOpen(false)}
        title="Stop the running job?"
        footer={
          <>
            <Button onClick={() => setStopOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={stopJob}>
              <XCircle size={15} /> Stop
            </Button>
          </>
        }
      >
        The current account finishes, then the job stops. Results so far are kept.
      </Dialog>

      {toast && (
        <div className="toast" style={{ padding: '12px 26px', fontSize: 14 }} role="status" aria-live="polite">
          {toast}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, tone, small, icon: Icon, hint }) {
  const color =
    tone === 'ok'
      ? 'var(--success-deep)'
      : tone === 'bad'
        ? 'var(--danger-deep)'
        : 'var(--text)'
  return (
    <div className="status-stat">
      <div className="status-stat-head">
        <Icon size={14} className="status-stat-icon" style={{ color }} />
        <span className="status-stat-label">{label}</span>
      </div>
      <div
        className={small ? 'status-stat-value is-small' : 'status-stat-value'}
        style={{ color }}
      >
        {value}
      </div>
      {hint && <div className="status-stat-hint">{hint}</div>}
    </div>
  )
}

const styles = {
  wrap: {
    display: 'flex',
    flexDirection: 'column',
    gap: 14,
    maxWidth: 980,
    width: '100%',
    margin: '0 auto',
  },
  hero: {
    padding: 'clamp(18px, 3vw, 26px)',
    display: 'flex',
    gap: 16,
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
  },
  heroText: { flex: '1 1 260px', minWidth: 0 },
  eyebrow: {
    fontSize: 11.5,
    color: 'var(--text-3)',
    fontWeight: 700,
    letterSpacing: 0.6,
    marginBottom: 6,
  },
  heroTitle: {
    fontSize: 'clamp(20px, 4.5vw, 26px)',
    fontWeight: 800,
    letterSpacing: -0.4,
    lineHeight: 1.2,
    color: 'var(--text)',
  },
  heroSub: {
    margin: '8px 0 0',
    fontSize: 13.5,
    lineHeight: 1.5,
    color: 'var(--text-3)',
    maxWidth: 520,
  },
  metricsCard: { padding: 0, overflow: 'hidden' },
  hitBreakdown: {
    display: 'flex',
    gap: 6,
    flexWrap: 'wrap',
    padding: '10px 20px 14px',
    borderTop: '1px solid var(--border)',
  },
  progressCard: { padding: '16px 20px' },
  progressHead: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    fontSize: 12.5,
    marginBottom: 10,
    flexWrap: 'wrap',
    gap: 8,
  },
  progressTrack: {
    height: 10,
    borderRadius: 4,
    background: 'var(--surface-2)',
    overflow: 'hidden',
  },
  progressFill: {
    height: '100%',
    borderRadius: 4,
    transition: 'background 0.3s',
    willChange: 'transform',
    transformOrigin: 'left center',
  },
  progressLegend: {
    display: 'flex',
    gap: 14,
    flexWrap: 'wrap',
    marginTop: 10,
    fontSize: 12,
    fontWeight: 600,
  },
}
