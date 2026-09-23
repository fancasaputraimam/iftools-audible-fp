import React, { useEffect, useRef, useState } from 'react'
import {
  AudioLines,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock3,
  Copy,
  Download,
  Eye,
  EyeOff,
  FileText,
  Filter,
  FolderOpen,
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
} from 'lucide-react'
import { api, subscribeLogs } from '../api.js'
import { Badge, Button, Card, Dialog, Input, Spinner } from './ui.jsx'

const SPEEDS = [
  { id: 'slow', label: 'Slow' },
  { id: 'normal', label: 'Normal' },
  { id: 'fast', label: 'Fast' },
  { id: 'maximum', label: 'Maximum' },
]

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'ok', label: 'Hits' },
  { id: 'check', label: 'Check' },
  { id: 'fail', label: 'Fail' },
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

const STATUS_META = {
  ok: { label: 'Hit', tone: 'success', row: 'row-ok' },
  check: { label: 'Check', tone: 'warning', row: 'row-check' },
  fail: { label: 'Fail', tone: 'danger', row: 'row-fail' },
}

function StatusChip({ s, note }) {
  const n = (note || '').toLowerCase()
  if (s === 'ok') {
    if (n.startsWith('v2l')) return <Badge tone="success">v2l ✓</Badge>
    if (n.startsWith('dcq:zip')) return <Badge tone="success">DCQ ZIP</Badge>
    if (n.startsWith('dcq:name')) return <Badge tone="success">DCQ Name</Badge>
    if (n.startsWith('dcq:phone')) return <Badge tone="success">DCQ Phone</Badge>
    if (n.startsWith('dcq:')) return <Badge tone="success">DCQ</Badge>
    if (n.startsWith('cc:')) return <Badge tone="success">CC Expiry</Badge>
    if (n.startsWith('push_notif')) return <Badge tone="success">Push</Badge>
    if (n.startsWith('otp_sms') || n.startsWith('otp_email')) return <Badge tone="success">OTP ✓</Badge>
    if (n.startsWith('otp_wa')) return <Badge tone="success">OTP WA ✓</Badge>
    return <Badge tone="success">Hit ✓</Badge>
  }
  if (s === 'fail') {
    if (n.includes('no_otp')) return <Badge tone="warning">No OTP</Badge>
    if (n.includes('not_amazon')) return <Badge tone="muted">Not Amazon</Badge>
    if (n.includes('amazon_error')) return <Badge tone="warning">Amz Err</Badge>
    if (n.includes('no_forgot')) return <Badge tone="muted">No URL</Badge>
    return <Badge tone="danger">Fail</Badge>
  }
  // check
  if (n.startsWith('v2l')) return <Badge tone="success">v2l ✓</Badge>
  if (n.startsWith('dcq')) return <Badge tone="info">DCQ?</Badge>
  if (n.startsWith('cc:')) return <Badge tone="warning">CC</Badge>
  if (n.startsWith('push_notif')) return <Badge tone="muted">Push?</Badge>
  if (n.startsWith('otp_sms')) return <Badge tone="muted">OTP SMS?</Badge>
  return <Badge tone="warning">Check</Badge>
}

function countParsed(text) {
  return text
    .split('\n')
    .filter((l) => {
      const t = l.trim()
      return t.length > 0 && !t.startsWith('#')
    }).length
}

// PasswordCell: masked by default, click to reveal
function PasswordCell({ password, onCopy }) {
  const [shown, setShown] = useState(false)
  return (
    <span className="pw-cell">
      <button
        type="button"
        className="copy-toggle"
        onClick={() => setShown((v) => !v)}
        title={shown ? 'Hide password' : 'Reveal password'}
      >
        {shown ? <EyeOff size={12} /> : <Eye size={12} />}
        <code style={{ fontSize: 12, marginLeft: 5 }}>
          {shown ? (password || '—') : '••••••••'}
        </code>
      </button>
      <button
        type="button"
        className="copy-btn"
        onClick={onCopy}
        title="Copy credential"
        aria-label="Copy email:password"
      >
        <Copy size={13} />
      </button>
    </span>
  )
}

export default function AudiblePanel() {
  const [accounts, setAccounts] = useState('')
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
  const [checkCount, setCheckCount] = useState(0)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [stopOpen, setStopOpen] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [filter, setFilter] = useState('all')
  const [sortDesc, setSortDesc] = useState(true)
  const pollRef = useRef(null)
  const toastTimer = useRef(null)
  const tickTimer = useRef(null)

  // SSE logs
  const [logLines, setLogLines] = useState([])
  const [logLive, setLogLive] = useState(false)
  const [logFollow, setLogFollow] = useState(true)
  const logBoxRef = useRef(null)
  const logUnsubRef = useRef(() => {})

  useEffect(() => {
    let closed = false
    api.get('/api/logs/snapshot?limit=500')
      .then((data) => {
        if (closed) return
        setLogLines(data.lines || [])
        setLogLive(true)
        subscribeLogs(data.seq || 0, (line) => {
          if (closed) return
          setLogLines((prev) => [...prev.slice(-1499), line])
        }).then((unsub) => {
          if (!closed) logUnsubRef.current = unsub
        }).catch(() => {})
      }).catch(() => {})
    return () => { closed = true; logUnsubRef.current(); setLogLive(false) }
  }, [])

  useEffect(() => {
    const el = logBoxRef.current
    if (!logFollow || !el) return
    el.scrollTop = el.scrollHeight
  }, [logLines, logFollow])

  function logTone(line) {
    return line.includes('[+]') ? 'success'
      : line.includes('[-]') || line.includes('[!]') ? 'danger'
      : line.includes('[*]') ? 'accent' : ''
  }

  function showToast(msg) {
    setToast(msg)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(''), 2600)
  }

  useEffect(() => {
    tickTimer.current = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tickTimer.current)
  }, [])

  useEffect(() => {
    if (!running) { clearInterval(pollRef.current); return undefined }
    const tick = () => {
      api.get('/api/audible/status').then((d) => {
        setStatus(d)
        setResults(d.results || [])
        setHitCount(d.hits || 0)
        setFailCount(d.fails || 0)
        setCheckCount(d.checks || 0)
        if (!d.running) {
          setRunning(false)
          showToast(d.done ? 'Job complete' : 'Job stopped')
        }
      }).catch(() => {})
    }
    tick()
    pollRef.current = setInterval(tick, 1200)
    return () => clearInterval(pollRef.current)
  }, [running])

  useEffect(() => {
    api.get('/api/audible/status').then((d) => {
      if (d && d.running) {
        setRunning(true); setStatus(d); setResults(d.results || [])
      } else if (d && Array.isArray(d.results) && d.results.length) {
        setResults(d.results)
        setHitCount(d.hits || 0)
        setFailCount(d.fails || 0)
        setCheckCount(d.checks || 0)
      }
    }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  async function onPickAccounts(e) {
    const f = e.target.files?.[0]; if (!f) return
    const text = await f.text()
    setAccounts(text); setFileName(f.name)
    showToast(`${f.name} — ${countParsed(text)} accounts`)
  }

  async function onPickProxies(e) {
    const f = e.target.files?.[0]; if (!f) return
    const text = await f.text()
    setProxyFile(text); setProxyName(f.name)
    const sample = text.split('\n').find((l) => l.trim() && !l.startsWith('#'))?.trim() || ''
    setProxySample(sample)
    showToast(`${f.name} — ${countParsed(text)} proxies`)
  }

  async function startJob() {
    if (busy) return
    const list = accounts.split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#') && l.includes(':'))
    if (!list.length) { showToast('Load an accounts.txt first (email:password)'); return }
    if (!proxyFile) { showToast('Proxy list required'); return }
    setBusy(true)
    try {
      await api.post('/api/audible/start', {
        accounts: list, speed,
        proxy_file: proxyFile || undefined,
        workers: workers ? Number(workers) : undefined,
        limit: limit ? Number(limit) : undefined,
      })
      setRunning(true)
      showToast(`Started — ${list.length} accounts · ${speed}`)
    } catch (e) { showToast(`Start failed: ${e.message}`) }
    finally { setBusy(false) }
  }

  async function stopJob() {
    setStopOpen(false)
    try { await api.post('/api/audible/stop', {}); showToast('Stop requested') }
    catch (e) { showToast(`Stop failed: ${e.message}`) }
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
      a.click(); URL.revokeObjectURL(url)
    } catch (e) { showToast(`Download failed: ${e.message}`) }
  }

  async function copyRow(email, password) {
    const txt = password ? `${email}:${password}` : email
    try { await navigator.clipboard.writeText(txt); showToast('Copied') }
    catch { showToast('Copy failed') }
  }

  // derived
  const total = results.length
  const target = status?.total || 0
  const processed = hitCount + failCount + checkCount
  const pct = target ? Math.min(100, Math.round((processed / target) * 100)) : 0

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
      if (hitCount === 0 && failCount > 0) return { label: 'All failed', tone: 'bad', title: 'No hits found' }
      if (target > 0 && processed < target) return { label: 'Stopped', tone: 'muted', title: 'Job stopped early' }
      return { label: 'Completed', tone: 'ok', title: 'Job completed' }
    }
    return { label: 'Ready', tone: 'muted', title: 'Ready to check' }
  })()

  // Filtered + sorted rows
  const filteredResults = (() => {
    let rows = filter === 'all' ? results : results.filter((r) => r.status === filter)
    if (!sortDesc) rows = [...rows].reverse()
    return rows
  })()

  return (
    <div style={styles.wrap}>

      {/* Hero */}
      <Card style={styles.hero}>
        <div style={styles.heroText}>
          <div style={styles.eyebrow}>AUDIBLE FP CHECKER</div>
          <h1 style={styles.heroTitle}>{statusInfo.title}</h1>
          <p style={styles.heroSub}>
            Audible.de forgot-password validator. Fingerprint stealth, IMAP OTP
            retrieval and proxy rotation run server-side.
          </p>
        </div>
        <Badge
          tone={statusInfo.tone === 'ok' ? 'success' : statusInfo.tone === 'bad' ? 'danger' : 'muted'}
          className="status-hero-badge"
        >
          {running && <span className="pulse-dot" />}
          {statusInfo.label}
        </Badge>
      </Card>

      {/* Metrics */}
      <Card style={styles.metricsCard}>
        <div className="status-metrics">
          <Stat label="Target"    value={target || '-'} icon={Target} />
          <Stat label="Hits"      value={hitCount}  tone="ok"  icon={CheckCircle2} />
          <Stat label="Check"     value={checkCount} tone="warn" icon={Filter} />
          <Stat label="Fails"     value={failCount}  tone="bad" icon={XCircle} />
          <Stat label="Started"   value={fmtTime(status?.started_at)} icon={Play} small />
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
            {otpCount > 0 && <Badge tone="muted">OTP: {otpCount}</Badge>}
            {hitCount - v2lCount - dcqCount - otpCount > 0 && (
              <Badge tone="muted">Other: {hitCount - v2lCount - dcqCount - otpCount}</Badge>
            )}
          </div>
        )}
      </Card>

      {/* Progress bar */}
      {target > 0 && (
        <Card style={styles.progressCard}>
          <div style={styles.progressHead}>
            <span style={{ color: 'var(--text-3)', fontWeight: 600 }}>Progress</span>
            <span style={{ fontWeight: 700 }}>
              {processed} / {target}{' '}
              <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>({pct}%)</span>
            </span>
          </div>
          <div style={styles.progressTrack}>
            <div
              role="progressbar"
              aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
              style={{
                ...styles.progressFill,
                width: `${pct}%`,
                background: statusInfo.tone === 'bad' ? 'var(--danger)'
                  : !running && statusInfo.tone === 'muted' ? 'var(--border-strong)'
                  : 'var(--sage)',
              }}
            />
          </div>
          {(hitCount > 0 || failCount > 0 || checkCount > 0) && (
            <div style={styles.progressLegend}>
              {hitCount > 0 && <span style={{ color: 'var(--success-deep)' }}>● {hitCount} hits</span>}
              {checkCount > 0 && <span style={{ color: 'var(--warning-deep)' }}>● {checkCount} check</span>}
              {failCount > 0 && <span style={{ color: 'var(--danger-deep)' }}>● {failCount} fails</span>}
              {running && target - processed > 0 && (
                <span style={{ color: 'var(--text-3)' }}>● {target - processed} pending</span>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Controls */}
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div className="ui-card-strip"><ListChecks size={15} /><strong>Configure Run</strong></div>
        <div className="run-steps">

          <div className="run-step">
            <div className="run-step-num">1</div>
            <div className="run-step-body">
              <div className="run-step-title">Upload files</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">Accounts <span className="run-field-required">required</span></div>
                  <label className="run-file-btn">
                    <FolderOpen size={14} />
                    <span>{fileName || 'Choose accounts.txt'}</span>
                    <input type="file" accept=".txt" onChange={onPickAccounts} hidden />
                  </label>
                  {accounts
                    ? <div className="run-field-hint ok"><code>{countParsed(accounts)} accounts · email:password</code></div>
                    : <div className="run-field-hint">One <code>email:password</code> per line</div>
                  }
                </div>
                <div className="run-field-group">
                  <div className="run-field-label">Proxies <span className="run-field-required">required</span></div>
                  <label className="run-file-btn">
                    <Upload size={14} />
                    <span>{proxyName || 'Choose proxies.txt'}</span>
                    <input type="file" accept=".txt" onChange={onPickProxies} hidden />
                  </label>
                  {proxyFile
                    ? <div className="run-field-hint ok"><code>{countParsed(proxyFile)} proxies{proxySample ? ` · ${proxySample.slice(0, 36)}…` : ''}</code></div>
                    : <div className="run-field-hint">Format: <code>http://user:pass@host:port</code></div>
                  }
                </div>
              </div>
            </div>
          </div>

          <div className="run-step-divider" />

          <div className="run-step">
            <div className="run-step-num">2</div>
            <div className="run-step-body">
              <div className="run-step-title">Configure</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">Speed</div>
                  <div className="glass-segmented" role="group" aria-label="Speed">
                    {SPEEDS.map((s) => (
                      <button key={s.id} type="button" className={speed === s.id ? 'active' : ''}
                        aria-pressed={speed === s.id} onClick={() => setSpeed(s.id)}>
                        {s.label}
                      </button>
                    ))}
                  </div>
                  <div className="run-field-hint">
                    {speed === 'maximum' ? '⚠ Needs stable proxies'
                      : speed === 'slow' ? '~1 acc/min, max accuracy'
                      : speed === 'normal' ? '~4–6 acc/min (recommended)'
                      : '~8–10 acc/min'}
                  </div>
                </div>
                <div className="run-field-group">
                  <label htmlFor="audible-workers" className="run-field-label">
                    Workers
                    <span className="run-field-tooltip" title="1 = most accurate">ⓘ</span>
                  </label>
                  <Input id="audible-workers" type="number" min="1" max="16" placeholder="Auto"
                    value={workers} onChange={(e) => setWorkers(e.target.value)}
                    className="status-count-input" autoComplete="off"
                    name="audible-workers-x" data-lpignore="true" data-1p-ignore="true" />
                  <div className="run-field-hint">1 = best accuracy</div>
                </div>
                <div className="run-field-group">
                  <label htmlFor="audible-limit" className="run-field-label">
                    Limit
                    <span className="run-field-tooltip" title="Only check first N accounts">ⓘ</span>
                  </label>
                  <Input id="audible-limit" type="number" min="1" placeholder="All"
                    value={limit} onChange={(e) => setLimit(e.target.value)}
                    className="status-count-input" autoComplete="off"
                    name="audible-limit-x" data-lpignore="true" data-1p-ignore="true" />
                  <div className="run-field-hint">Leave blank for all</div>
                </div>
              </div>
            </div>
          </div>

          <div className="run-step-divider" />

          <div className="run-step">
            <div className="run-step-num">3</div>
            <div className="run-step-body">
              <div className="run-step-title">Run</div>
              <div className="status-actions" style={{ flexWrap: 'wrap' }}>
                {running ? (
                  <Button variant="destructive" className="status-action-stop" onClick={() => setStopOpen(true)}>
                    <Pause size={15} /> Stop
                  </Button>
                ) : (
                  <Button variant="primary" className="status-action-primary" onClick={startJob}
                    disabled={busy || !accounts || !proxyFile}
                    title={!accounts ? 'Load accounts.txt first' : !proxyFile ? 'Load proxies.txt first' : ''}>
                    {busy ? <Spinner /> : <Play size={16} />} Start checker
                  </Button>
                )}
                <Button className="status-action-nav" onClick={() => download('hits')}
                  disabled={!hitCount} title="Download hits only">
                  <Download size={15} /> Hits ({hitCount})
                </Button>
                <Button className="status-action-nav" onClick={() => download('all')}
                  disabled={!total} title="Download all results">
                  <FileText size={15} /> All ({total})
                </Button>
              </div>
            </div>
          </div>
        </div>
      </Card>

      {/* Results table */}
      {total > 0 ? (
        <Card style={{ padding: 0, overflow: 'hidden' }}>
          {/* Table header with filter tabs + sort toggle */}
          <div className="results-header">
            <div className="results-title"><Gauge size={15} /><strong>Results</strong></div>
            <div className="results-toolbar">
              <div className="filter-tabs" role="group" aria-label="Filter results">
                {FILTERS.map((f) => {
                  const count = f.id === 'all' ? total
                    : f.id === 'ok' ? hitCount
                    : f.id === 'check' ? checkCount
                    : failCount
                  return (
                    <button key={f.id} type="button"
                      className={`filter-tab${filter === f.id ? ' active' : ''}${f.id === 'ok' ? ' tab-ok' : f.id === 'check' ? ' tab-check' : f.id === 'fail' ? ' tab-fail' : ''}`}
                      onClick={() => setFilter(f.id)}>
                      {f.label}
                      <span className="filter-tab-count">{count}</span>
                    </button>
                  )
                })}
              </div>
              <button type="button" className="sort-btn" onClick={() => setSortDesc((v) => !v)}
                title={sortDesc ? 'Newest first (click for oldest)' : 'Oldest first (click for newest)'}>
                {sortDesc ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
                {sortDesc ? 'Newest' : 'Oldest'}
              </button>
            </div>
          </div>

          {filteredResults.length === 0 ? (
            <div className="ui-empty-state" style={{ minHeight: 140 }}>
              <ListEnd size={24} />
              <strong>No {filter !== 'all' ? filter : ''} results</strong>
            </div>
          ) : (
            <div className="accounts-table-wrap">
              <table className="accounts-table">
                <thead>
                  <tr>
                    <th style={{ width: 40 }}>#</th>
                    <th>Email</th>
                    <th>Password</th>
                    <th style={{ width: 110 }}>Status</th>
                    <th>Note</th>
                    <th style={{ width: 48 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredResults.map((r, i) => (
                    <tr key={`${r.email}-${i}`} className={STATUS_META[r.status]?.row || ''}>
                      <td data-label="#">{i + 1}</td>
                      <td data-label="Email">
                        <code style={{ fontSize: 12 }}>{r.email}</code>
                      </td>
                      <td data-label="Password">
                        <PasswordCell
                          password={r.password}
                          onCopy={() => copyRow(r.email, r.password)}
                        />
                      </td>
                      <td data-label="Status">
                        <StatusChip s={r.status} note={r.note} />
                      </td>
                      <td data-label="Note">
                        <span className="note-text">{r.note || '—'}</span>
                      </td>
                      <td data-label="Copy">
                        <button type="button" className="copy-btn"
                          onClick={() => copyRow(r.email, r.password)}
                          title="Copy email:password" aria-label="Copy">
                          <Copy size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ) : (
        <Card>
          <div className="ui-empty-state">
            <AudioLines size={28} />
            <strong>No results yet</strong>
            <p>Load an accounts.txt, pick a proxy list, then start the checker.</p>
          </div>
        </Card>
      )}

      {/* Log stream */}
      <Card className="log-layout" style={{ maxWidth: '100%' }}>
        <div className="log-toolbar">
          <div className="log-toolbar-group">
            <Badge tone={logLive ? 'success' : 'muted'}>
              <Radio size={12} />{logLive ? 'Streaming' : 'Connecting'}
            </Badge>
            <span>{logLines.length} lines</span>
          </div>
          <div className="log-toolbar-group">
            <label className="log-follow">
              <input type="checkbox" checked={logFollow} onChange={(e) => setLogFollow(e.target.checked)} />
              Auto-scroll
            </label>
            <Button size="sm" onClick={() => setLogLines([])}><Trash2 size={14} /> Clear</Button>
          </div>
        </div>
        <div className="log-terminal" ref={logBoxRef}>
          {logLines.length === 0 ? (
            <div className="ui-empty-state">
              <ScrollText size={26} />
              <strong>No logs yet</strong>
              <p>Start a run and every checker step streams here.</p>
            </div>
          ) : (
            logLines.map((line, index) => (
              <div key={index} className={`log-line ${logTone(line)}`}>{line}</div>
            ))
          )}
        </div>
      </Card>

      <Dialog open={stopOpen} onClose={() => setStopOpen(false)} title="Stop the running job?"
        footer={<><Button onClick={() => setStopOpen(false)}>Cancel</Button>
          <Button variant="destructive" onClick={stopJob}><XCircle size={15} /> Stop</Button></>}>
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
  const color = tone === 'ok' ? 'var(--success-deep)'
    : tone === 'bad' ? 'var(--danger-deep)'
    : tone === 'warn' ? 'var(--warning-deep)'
    : 'var(--text)'
  return (
    <div className="status-stat">
      <div className="status-stat-head">
        <Icon size={14} className="status-stat-icon" style={{ color }} />
        <span className="status-stat-label">{label}</span>
      </div>
      <div className={small ? 'status-stat-value is-small' : 'status-stat-value'} style={{ color }}>
        {value}
      </div>
      {hint && <div className="status-stat-hint">{hint}</div>}
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 980, width: '100%', margin: '0 auto' },
  hero: { padding: 'clamp(18px,3vw,26px)', display: 'flex', gap: 16, alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap' },
  heroText: { flex: '1 1 260px', minWidth: 0 },
  eyebrow: { fontSize: 11.5, color: 'var(--text-3)', fontWeight: 700, letterSpacing: 0.6, marginBottom: 6 },
  heroTitle: { fontSize: 'clamp(20px,4.5vw,26px)', fontWeight: 800, letterSpacing: -0.4, lineHeight: 1.2, color: 'var(--text)' },
  heroSub: { margin: '8px 0 0', fontSize: 13.5, lineHeight: 1.5, color: 'var(--text-3)', maxWidth: 520 },
  metricsCard: { padding: 0, overflow: 'hidden' },
  hitBreakdown: { display: 'flex', gap: 6, flexWrap: 'wrap', padding: '10px 20px 14px', borderTop: '1px solid var(--border)' },
  progressCard: { padding: '16px 20px' },
  progressHead: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 12.5, marginBottom: 10, flexWrap: 'wrap', gap: 8 },
  progressTrack: { height: 10, borderRadius: 4, background: 'var(--surface-2)', overflow: 'hidden' },
  progressFill: { height: '100%', borderRadius: 4, transition: 'background 0.3s', willChange: 'transform', transformOrigin: 'left center' },
  progressLegend: { display: 'flex', gap: 14, flexWrap: 'wrap', marginTop: 10, fontSize: 12, fontWeight: 600 },
}
