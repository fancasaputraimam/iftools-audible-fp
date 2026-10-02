import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AudioLines,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
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
  Search,
  Square,
  Target,
  Trash2,
  Upload,
  XCircle,
} from 'lucide-react'
import { api, subscribeLogs } from '../api.js'
import { Badge, Button, Card, Dialog, Input, Spinner } from './ui.jsx'
import DownloadModal from './DownloadModal.jsx'

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const SPEEDS = [
  { id: 'slow', label: 'Slow', hint: '~1 acc/min, max accuracy' },
  { id: 'normal', label: 'Normal', hint: '~4–6 acc/min (recommended)' },
  { id: 'fast', label: 'Fast', hint: '~8–10 acc/min' },
  { id: 'maximum', label: 'Maximum', hint: '⚠ Needs stable proxies' },
]

const RESULT_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'ok', label: 'Hits' },
  { id: 'check', label: 'Check' },
  { id: 'fail', label: 'Fail' },
]

const VERIF_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'v2l', label: 'V2L', match: (n) => n.startsWith('v2l') },
  { id: 'dcq', label: 'DCQ', match: (n) => n.startsWith('dcq') },
  { id: 'otp', label: 'OTP', match: (n) => n.startsWith('otp_') || n === 'otp' },
  { id: 'cc', label: 'CC', match: (n) => n.startsWith('cc:') },
  { id: 'push', label: 'Push', match: (n) => n.startsWith('push_notif') },
  { id: 'not_amazon', label: 'Not Amazon', match: (n) => n.includes('not_amazon') },
  { id: 'no_otp', label: 'No OTP', match: (n) => n.includes('no_otp') },
]

const STATUS_TONE = {
  ok:    { label: 'Hit',   tone: 'success', row: 'row-ok' },
  check: { label: 'Check', tone: 'warning', row: 'row-check' },
  fail:  { label: 'Fail',  tone: 'danger',  row: 'row-fail' },
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: false }) : '–'
}

function fmtDuration(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '–'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${pad(h)}.${pad(m)}.${pad(r)}` : `${pad(m)}.${pad(r)}`
}

function countLines(text) {
  return text.split('\n').filter((l) => {
    const t = l.trim()
    return t.length > 0 && !t.startsWith('#')
  }).length
}

/**
 * Auto-clean account lines: extract email:password from messy formats.
 * Handles: "email:pass - note", "email:pass (label)", "email:pass | Name: x"
 * Returns cleaned text with one "email:pass" per line.
 */
function cleanAccountLines(text) {
  const lines = text.split('\n')
  const cleaned = []
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    // Match email:password at the start, stop at whitespace or separator
    const m = line.match(/^([^\s:@]+@[^\s:@]+):(\S+?)(?:\s+[-|(].*|$)/)
    if (m) {
      cleaned.push(`${m[1]}:${m[2]}`)
    } else if (line.includes(':')) {
      // Fallback: take everything before first space/tab/pipe/dash-with-space
      const parts = line.split(':')
      const email = parts[0].trim()
      const rest = parts.slice(1).join(':').trim()
      const pass = rest.split(/\s+[-|(]/)[0].split(/\s{2,}/)[0].trim()
      if (email && pass) cleaned.push(`${email}:${pass}`)
    }
  }
  return cleaned.join('\n')
}

function logTone(line) {
  if (line.includes('[+]')) return 'success'
  if (line.includes('[-]') || line.includes('[!]')) return 'danger'
  if (line.includes('[*]')) return 'accent'
  return ''
}

/* ------------------------------------------------------------------ */
/*  Hooks                                                              */
/* ------------------------------------------------------------------ */

function useToast(duration = 2600) {
  const [msg, setMsg] = useState('')
  const timer = useRef(null)

  const show = useCallback((text) => {
    setMsg(text)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setMsg(''), duration)
  }, [duration])

  useEffect(() => () => clearTimeout(timer.current), [])
  return [msg, show]
}

function useTick(ms = 1000) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}

function useAudibleJob(showToast) {
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState([])
  const [hits, setHits] = useState(0)
  const [fails, setFails] = useState(0)
  const [checks, setChecks] = useState(0)
  const pollRef = useRef(null)

  const sync = useCallback((d) => {
    setStatus(d)
    setResults(d.results || [])
    setHits(d.hits || 0)
    setFails(d.fails || 0)
    setChecks(d.checks || 0)
  }, [])

  /* Poll while running */
  useEffect(() => {
    if (!running) { clearInterval(pollRef.current); return }
    const tick = () => {
      api.get('/api/audible/status').then((d) => {
        sync(d)
        if (!d.running) {
          setRunning(false)
          showToast(d.done ? 'Job complete' : 'Job stopped')
        }
      }).catch(() => {})
    }
    tick()
    pollRef.current = setInterval(tick, 1200)
    return () => clearInterval(pollRef.current)
  }, [running, sync, showToast])

  /* Restore on mount */
  useEffect(() => {
    api.get('/api/audible/status').then((d) => {
      if (d?.running) { setRunning(true); sync(d) }
      else if (d?.results?.length) sync(d)
    }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [sync])

  return { running, setRunning, status, results, hits, fails, checks }
}

function useLogs() {
  const [lines, setLines] = useState([])
  const [live, setLive] = useState(false)
  const [follow, setFollow] = useState(true)
  const boxRef = useRef(null)
  const unsubRef = useRef(() => {})

  useEffect(() => {
    let closed = false
    api.get('/api/logs/snapshot?limit=500').then((data) => {
      if (closed) return
      setLines(data.lines || [])
      setLive(true)
      subscribeLogs(data.seq || 0, (line) => {
        if (closed) return
        setLines((prev) => [...prev.slice(-1499), line])
      }).then((unsub) => {
        if (!closed) unsubRef.current = unsub
      }).catch(() => {})
    }).catch(() => {})
    return () => { closed = true; unsubRef.current(); setLive(false) }
  }, [])

  useEffect(() => {
    if (!follow || !boxRef.current) return
    boxRef.current.scrollTop = boxRef.current.scrollHeight
  }, [lines, follow])

  return { lines, setLines, live, follow, setFollow, boxRef }
}

/* ------------------------------------------------------------------ */
/*  Sub-components                                                     */
/* ------------------------------------------------------------------ */

function StatusChip({ status, note }) {
  const n = (note || '').toLowerCase()

  if (status === 'ok') {
    if (n.startsWith('v2l'))        return <Badge tone="success">v2l ✓</Badge>
    if (n.startsWith('dcq:zip'))    return <Badge tone="success">DCQ ZIP</Badge>
    if (n.startsWith('dcq:name'))   return <Badge tone="success">DCQ Name</Badge>
    if (n.startsWith('dcq:phone'))  return <Badge tone="success">DCQ Phone</Badge>
    if (n.startsWith('dcq:'))       return <Badge tone="success">DCQ</Badge>
    if (n.startsWith('cc:'))        return <Badge tone="success">CC Expiry</Badge>
    if (n.startsWith('push_notif')) return <Badge tone="success">Push</Badge>
    if (n.startsWith('otp_sms') || n.startsWith('otp_email')) return <Badge tone="success">OTP ✓</Badge>
    if (n.startsWith('otp_wa'))     return <Badge tone="success">OTP WA ✓</Badge>
    return <Badge tone="success">Hit ✓</Badge>
  }

  if (status === 'fail') {
    if (n.includes('no_otp'))       return <Badge tone="warning">No OTP</Badge>
    if (n.includes('not_amazon'))   return <Badge tone="muted">Not Amazon</Badge>
    if (n.includes('amazon_error')) return <Badge tone="warning">Amz Err</Badge>
    if (n.includes('no_forgot'))    return <Badge tone="muted">No URL</Badge>
    return <Badge tone="danger">Fail</Badge>
  }

  /* check */
  if (n.startsWith('v2l'))        return <Badge tone="success">v2l ✓</Badge>
  if (n.startsWith('dcq'))        return <Badge tone="info">DCQ?</Badge>
  if (n.startsWith('cc:'))        return <Badge tone="warning">CC</Badge>
  if (n.startsWith('push_notif')) return <Badge tone="muted">Push?</Badge>
  if (n.startsWith('otp_sms'))    return <Badge tone="muted">OTP SMS?</Badge>
  if (n.startsWith('otp_wa'))     return <Badge tone="muted">OTP WA?</Badge>
  if (n.startsWith('otp_email'))  return <Badge tone="muted">OTP Email?</Badge>
  if (n === 'otp')                return <Badge tone="muted">OTP?</Badge>
  return <Badge tone="warning">Check</Badge>
}

function PasswordCell({ password, onCopy }) {
  const [shown, setShown] = useState(false)
  return (
    <span className="pw-cell">
      <button type="button" className="copy-toggle"
        onClick={() => setShown((v) => !v)}
        title={shown ? 'Hide password' : 'Reveal password'}>
        {shown ? <EyeOff size={12} /> : <Eye size={12} />}
        <code className="pw-value">{shown ? (password || '—') : '••••••••'}</code>
      </button>
      <button type="button" className="copy-btn" onClick={onCopy}
        title="Copy credential" aria-label="Copy email:password">
        <Copy size={13} />
      </button>
    </span>
  )
}

function Stat({ label, value, tone, small, icon: Icon }) {
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
      <div className={`status-stat-value${small ? ' is-small' : ''}`} style={{ color }}>
        {value}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export default function AudiblePanel() {
  /* Toast */
  const [toast, showToast] = useToast()

  /* Job state */
  const { running, setRunning, status, results, hits, fails, checks } = useAudibleJob(showToast)

  /* Logs */
  const logs = useLogs()

  /* Local UI state */
  const [accounts, setAccounts] = useState('')
  const [fileName, setFileName] = useState('')
  const [speed, setSpeed] = useState('normal')
  const [proxyFile, setProxyFile] = useState('')
  const [proxyName, setProxyName] = useState('')
  const [proxySample, setProxySample] = useState('')
  const [workers, setWorkers] = useState('')
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [stopOpen, setStopOpen] = useState(false)
  const [dlOpen, setDlOpen] = useState(false)
  const [filter, setFilter] = useState('all')
  const [sortDesc, setSortDesc] = useState(true)
  const [configOpen, setConfigOpen] = useState(false)
  const [autoClean, setAutoClean] = useState(true)

  /* Clock for elapsed time */
  const now = useTick()

  /* ---- File pickers ---- */
  function onPickAccounts(e) {
    const f = e.target.files?.[0]
    if (!f) return
    f.text().then((text) => {
      const raw = countLines(text)
      if (autoClean) {
        const cleaned = cleanAccountLines(text)
        setAccounts(cleaned)
        const after = countLines(cleaned)
        setFileName(f.name)
        showToast(`${f.name} — ${after} accounts (cleaned from ${raw} lines)`)
      } else {
        setAccounts(text)
        setFileName(f.name)
        showToast(`${f.name} — ${raw} accounts`)
      }
    })
  }

  function onPickProxies(e) {
    const f = e.target.files?.[0]
    if (!f) return
    f.text().then((text) => {
      setProxyFile(text)
      setProxyName(f.name)
      const sample = text.split('\n').find((l) => l.trim() && !l.startsWith('#'))?.trim() || ''
      setProxySample(sample)
      showToast(`${f.name} — ${countLines(text)} proxies`)
    })
  }

  /* ---- Actions ---- */
  async function startJob() {
    if (busy) return
    const list = accounts.split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#') && l.includes(':'))
    if (!list.length) { showToast('Load an accounts.txt first (email:password)'); return }
    if (!proxyFile)   { showToast('Proxy list required'); return }
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
      showToast(`Started — ${list.length} accounts · ${speed}`)
    } catch (err) {
      showToast(`Start failed: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function stopJob() {
    setStopOpen(false)
    try {
      await api.post('/api/audible/stop', {})
      showToast('Stop requested')
    } catch (err) {
      showToast(`Stop failed: ${err.message}`)
    }
  }

  async function copyRow(email, password) {
    const txt = password ? `${email}:${password}` : email
    try { await navigator.clipboard.writeText(txt); showToast('Copied') }
    catch { showToast('Copy failed') }
  }

  /* ---- Derived data ---- */
  const total = results.length
  const target = status?.total || 0
  const processed = hits + fails + checks
  const pct = target ? Math.min(100, Math.round((processed / target) * 100)) : 0

  const breakdown = useMemo(() => {
    const v2l = results.filter((r) => r.note?.toLowerCase().startsWith('v2l')).length
    const dcq = results.filter((r) => r.note?.toLowerCase().startsWith('dcq')).length
    const otp = results.filter((r) => {
      const n = r.note?.toLowerCase() || ''
      return (n.startsWith('otp_sms') || n.startsWith('otp_email')) && r.status === 'ok'
    }).length
    const wa = results.filter((r) => r.note?.toLowerCase().startsWith('otp_wa')).length
    const captcha = results.filter((r) => r.note?.toLowerCase().startsWith('captcha')).length
    return { v2l, dcq, otp, wa, captcha }
  }, [results])

  const elapsedSec = useMemo(() => {
    if (!status?.started_at) return 0
    const end = status?.finished_at ? status.finished_at * 1000 : now
    return Math.max(0, (end - status.started_at * 1000) / 1000)
  }, [status, now])

  const statusInfo = useMemo(() => {
    if (running) return { label: 'Running', tone: 'ok', title: 'Validating accounts' }
    if (status?.error) return { label: 'Error', tone: 'bad', title: 'Job failed' }
    if (status?.finished_at) {
      if (hits === 0 && fails > 0) return { label: 'All failed', tone: 'bad', title: 'No hits found' }
      if (target > 0 && processed < target) return { label: 'Stopped', tone: 'muted', title: 'Job stopped early' }
      return { label: 'Completed', tone: 'ok', title: 'Job completed' }
    }
    return { label: 'Ready', tone: 'muted', title: 'Ready to check' }
  }, [running, status, hits, fails, target, processed])

  const filteredResults = useMemo(() => {
    let rows = filter === 'all' ? results : results.filter((r) => r.status === filter)
    if (!sortDesc) rows = [...rows].reverse()
    return rows
  }, [results, filter, sortDesc])

  const progressTone = statusInfo.tone === 'bad' ? 'var(--danger)'
    : !running && statusInfo.tone === 'muted' ? 'var(--border-strong)'
    : 'var(--sage)'

  /* ---- Render ---- */
  return (
    <div className="audible-panel">

      {/* ═══ Hero ═══ */}
      <Card className="panel-hero audible-hero">
        <div className="audible-hero-text">
          <div className="panel-eyebrow">AUDIBLE FP CHECKER</div>
          <h1 className="audible-hero-title">{statusInfo.title}</h1>
          <p className="audible-hero-sub">
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

      {/* ═══ Metrics strip ═══ */}
      <Card className="audible-metrics-card">
        <div className="status-metrics">
          <Stat label="Target"  value={target || '–'} icon={Target} />
          <Stat label="Hits"    value={hits}   tone="ok"   icon={CheckCircle2} />
          <Stat label="Check"   value={checks} tone="warn" icon={Filter} />
          <Stat label="Fails"   value={fails}  tone="bad"  icon={XCircle} />
          <Stat label="Started" value={fmtTime(status?.started_at)} icon={Play} small />
          <Stat label={running ? 'Elapsed' : 'Duration'} value={fmtDuration(elapsedSec)}
            icon={running ? Clock3 : Square} small />
        </div>
        {hits > 0 && (
          <div className="audible-hit-breakdown">
            {breakdown.v2l > 0 && <Badge tone="success">v2l: {breakdown.v2l}</Badge>}
            {breakdown.dcq > 0 && <Badge tone="info">DCQ: {breakdown.dcq}</Badge>}
            {breakdown.otp > 0 && <Badge tone="info">OTP: {breakdown.otp}</Badge>}
            {hits - breakdown.v2l - breakdown.dcq - breakdown.otp > 0 && (
              <Badge tone="warning">Other: {hits - breakdown.v2l - breakdown.dcq - breakdown.otp}</Badge>
            )}
          </div>
        )}
      </Card>

      {/* ═══ Progress bar ═══ */}
      {target > 0 && (
        <Card className="audible-progress-card">
          <div className="audible-progress-head">
            <span className="audible-progress-label">Progress</span>
            <span className="audible-progress-value">
              {processed} / {target}{' '}
              <span className="audible-progress-pct">({pct}%)</span>
            </span>
          </div>
          <div className="audible-progress-track" role="progressbar"
            aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            {hits > 0 && (
              <div className="audible-progress-seg seg-ok"
                style={{ width: `${(hits / target) * 100}%` }} />
            )}
            {checks > 0 && (
              <div className="audible-progress-seg seg-warn"
                style={{ width: `${(checks / target) * 100}%` }} />
            )}
            {fails > 0 && (
              <div className="audible-progress-seg seg-bad"
                style={{ width: `${(fails / target) * 100}%` }} />
            )}
          </div>
          {(hits > 0 || fails > 0 || checks > 0) && (
            <div className="audible-progress-legend">
              {hits > 0   && <span className="legend-ok">● {hits} hits</span>}
              {checks > 0 && <span className="legend-warn">● {checks} check</span>}
              {fails > 0  && <span className="legend-bad">● {fails} fails</span>}
              {running && target - processed > 0 && (
                <span className="legend-pending">● {target - processed} pending</span>
              )}
            </div>
          )}
        </Card>
      )}

      {/* ═══ Configure Run (collapsible) ═══ */}
      <Card className="audible-config-card">
        <button type="button" className="ui-card-strip config-toggle"
          onClick={() => setConfigOpen((v) => !v)} aria-expanded={configOpen}>
          <ListChecks size={15} />
          <strong>Configure Run</strong>
          <span className="config-toggle-chevron">
            {configOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </span>
        </button>

        {configOpen && (
          <div className="run-steps">
            {/* Step 1 — Upload */}
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
                      ? <div className="run-field-hint ok"><code>{countLines(accounts)} accounts · email:password</code></div>
                      : <div className="run-field-hint">One <code>email:password</code> per line</div>}
                    <label className="auto-clean-toggle">
                      <input type="checkbox" checked={autoClean}
                        onChange={(e) => setAutoClean(e.target.checked)} />
                      <span>Auto-clean (strip notes, labels, extra text — keep email:pass only)</span>
                    </label>
                  </div>
                  <div className="run-field-group">
                    <div className="run-field-label">Proxies <span className="run-field-required">required</span></div>
                    <label className="run-file-btn">
                      <Upload size={14} />
                      <span>{proxyName || 'Choose proxies.txt'}</span>
                      <input type="file" accept=".txt" onChange={onPickProxies} hidden />
                    </label>
                    {proxyFile
                      ? <div className="run-field-hint ok"><code>{countLines(proxyFile)} proxies{proxySample ? ` · ${proxySample.slice(0, 36)}…` : ''}</code></div>
                      : <div className="run-field-hint">Format: <code>http://user:pass@host:port</code></div>}
                  </div>
                </div>
              </div>
            </div>

            <div className="run-step-divider" />

            {/* Step 2 — Configure */}
            <div className="run-step">
              <div className="run-step-num">2</div>
              <div className="run-step-body">
                <div className="run-step-title">Configure</div>
                <div className="run-step-fields">
                  <div className="run-field-group">
                    <div className="run-field-label">Speed</div>
                    <div className="glass-segmented" role="group" aria-label="Speed">
                      {SPEEDS.map((s) => (
                        <button key={s.id} type="button"
                          className={speed === s.id ? 'active' : ''}
                          aria-pressed={speed === s.id}
                          onClick={() => setSpeed(s.id)}>
                          {s.label}
                        </button>
                      ))}
                    </div>
                    <div className="run-field-hint">
                      {SPEEDS.find((s) => s.id === speed)?.hint}
                    </div>
                  </div>
                  <div className="run-field-group">
                    <label htmlFor="audible-workers" className="run-field-label">
                      Workers <span className="run-field-tooltip" title="1 = most accurate">ⓘ</span>
                    </label>
                    <Input id="audible-workers" type="number" min="1" max="16" placeholder="Auto"
                      value={workers} onChange={(e) => setWorkers(e.target.value)}
                      className="status-count-input" autoComplete="off"
                      name="audible-workers-x" data-lpignore="true" data-1p-ignore="true" />
                    <div className="run-field-hint">1 = best accuracy</div>
                  </div>
                  <div className="run-field-group">
                    <label htmlFor="audible-limit" className="run-field-label">
                      Limit <span className="run-field-tooltip" title="Only check first N accounts">ⓘ</span>
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

            {/* Step 3 — Run */}
            <div className="run-step">
              <div className="run-step-num">3</div>
              <div className="run-step-body">
                <div className="run-step-title">Run</div>
                <div className="status-actions audible-run-actions">
                  {running ? (
                    <Button variant="destructive" className="status-action-stop"
                      onClick={() => setStopOpen(true)}>
                      <Pause size={15} /> Stop
                    </Button>
                  ) : (
                    <Button variant="primary" className="status-action-primary"
                      onClick={startJob}
                      disabled={busy || !accounts || !proxyFile}
                      title={!accounts ? 'Load accounts.txt first' : !proxyFile ? 'Load proxies.txt first' : ''}>
                      {busy ? <Spinner /> : <Play size={16} />} Start checker
                    </Button>
                  )}
                  <Button className="status-action-nav" onClick={() => setDlOpen(true)}
                    disabled={!total} title="Download results">
                    <Download size={15} /> Download
                  </Button>
                </div>
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* ═══ Results table ═══ */}
      <ResultsTable
        results={filteredResults}
        allResults={results}
        total={total}
        hits={hits}
        checks={checks}
        fails={fails}
        filter={filter}
        setFilter={setFilter}
        sortDesc={sortDesc}
        setSortDesc={setSortDesc}
        onCopy={copyRow}
      />

      {/* ═══ Log stream ═══ */}
      <LogTerminal logs={logs} />

      {/* ═══ Dialogs ═══ */}
      <Dialog open={stopOpen} onClose={() => setStopOpen(false)} title="Stop the running job?"
        footer={<>
          <Button onClick={() => setStopOpen(false)}>Cancel</Button>
          <Button variant="destructive" onClick={stopJob}><XCircle size={15} /> Stop</Button>
        </>}>
        The current account finishes, then the job stops. Results so far are kept.
      </Dialog>

      <DownloadModal
        open={dlOpen}
        onClose={() => setDlOpen(false)}
        prefix="audible"
        showToast={showToast}
        options={[
          { id: 'hits',  label: 'All hits',    hint: 'Every non-failed account',         count: hits,                      kind: 'hits',  tone: 'success' },
          { id: 'otp',   label: 'OTP hits',    hint: 'OTP retrieved — SMS / WA / email', count: breakdown.otp + breakdown.wa, kind: 'otp',   tone: 'success' },
          { id: 'v2l',   label: 'v2l',         hint: 'Valid login, not Amazon',           count: breakdown.v2l,             kind: 'v2l',   tone: 'info' },
          { id: 'dcq',   label: 'DCQ / Push',  hint: 'Needs device or card verification', count: breakdown.dcq,            kind: 'dcq',   tone: 'warning' },
          { id: 'check', label: 'Check',        hint: 'Inconclusive — manual look',       count: checks,                   kind: 'check', tone: 'warning' },
          { id: 'fail',  label: 'Failed',       hint: 'Wrong password, no OTP, etc.',      count: fails,                    kind: 'fail',  tone: 'danger' },
        ]}
        cols={[
          { id: 'all',    label: 'Everything', hint: 'All buckets combined',  kinds: ['all'] },
          { id: 'usable', label: 'All usable', hint: 'OTP + v2l + DCQ',      kinds: ['otp', 'v2l', 'dcq'] },
        ]}
      />

      {/* ═══ Toast ═══ */}
      {toast && (
        <div className="toast" role="status" aria-live="polite">{toast}</div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  ResultsTable                                                       */
/* ------------------------------------------------------------------ */

function ResultsTable({ results, allResults, total, hits, checks, fails, filter, setFilter, sortDesc, setSortDesc, onCopy }) {
  const [searchQ, setSearchQ] = useState('')
  const [verif, setVerif] = useState('all')

  const filtered = useMemo(() => {
    let rows = results
    if (verif !== 'all') {
      const vf = VERIF_FILTERS.find((v) => v.id === verif)
      if (vf?.match) rows = rows.filter((r) => vf.match((r.note || '').toLowerCase()))
    }
    if (searchQ.trim()) {
      const q = searchQ.trim().toLowerCase()
      rows = rows.filter((r) =>
        r.email.toLowerCase().includes(q) ||
        (r.password || '').toLowerCase().includes(q) ||
        (r.note || '').toLowerCase().includes(q)
      )
    }
    return rows
  }, [results, verif, searchQ])

  if (!total) {
    return (
      <Card>
        <div className="ui-empty-state">
          <AudioLines size={28} />
          <strong>No results yet</strong>
          <p>Load an accounts.txt, pick a proxy list, then start the checker.</p>
        </div>
      </Card>
    )
  }

  return (
    <Card className="audible-results-card">
      <div className="results-header">
        <div className="results-title"><Gauge size={15} /><strong>Results</strong></div>
        <div className="results-toolbar">
          <div className="filter-tabs" role="group" aria-label="Filter results">
            {RESULT_FILTERS.map((f) => {
              const count = f.id === 'all' ? total
                : f.id === 'ok' ? hits : f.id === 'check' ? checks : fails
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
          <button type="button" className="sort-btn"
            onClick={() => setSortDesc((v) => !v)}
            title={sortDesc ? 'Newest first (click for oldest)' : 'Oldest first (click for newest)'}>
            {sortDesc ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
            {sortDesc ? 'Newest' : 'Oldest'}
          </button>
        </div>
      </div>

      {/* Search + Verif filter */}
      <div className="results-search-row">
        <div className="results-search-box">
          <Search size={14} className="results-search-icon" />
          <input type="text" className="results-search-input"
            placeholder="Search email, password, note..."
            value={searchQ} onChange={(e) => setSearchQ(e.target.value)} />
          {searchQ && (
            <button type="button" className="results-search-clear"
              onClick={() => setSearchQ('')} aria-label="Clear search">
              <XCircle size={14} />
            </button>
          )}
        </div>
        <div className="verif-tabs" role="group" aria-label="Filter by verification type">
          {VERIF_FILTERS.map((v) => (
            <button key={v.id} type="button"
              className={`verif-tab${verif === v.id ? ' active' : ''}`}
              onClick={() => setVerif(v.id)}>
              {v.label}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="ui-empty-state" style={{ minHeight: 140 }}>
          <ListEnd size={24} />
          <strong>No {filter !== 'all' ? filter : ''} results{searchQ ? ` matching "${searchQ}"` : ''}{verif !== 'all' ? ` (${verif})` : ''}</strong>
        </div>
      ) : (
        <div className="accounts-table-wrap">
          <table className="accounts-table">
            <thead>
              <tr>
                <th style={{ width: 48, minWidth: 48 }}>#</th>
                <th>Email</th>
                <th>Password</th>
                <th style={{ width: 110 }}>Status</th>
                <th>Note</th>
                <th style={{ width: 48 }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r, i) => (
                <tr key={`${r.email}-${i}`} className={STATUS_TONE[r.status]?.row || ''}>
                  <td data-label="#">{i + 1}</td>
                  <td data-label="Email"><code className="email-cell" title={r.email}>{r.email}</code></td>
                  <td data-label="Password">
                    <PasswordCell password={r.password} onCopy={() => onCopy(r.email, r.password)} />
                  </td>
                  <td data-label="Status"><StatusChip status={r.status} note={r.note} /></td>
                  <td data-label="Note"><span className="note-text">{r.note || '—'}</span></td>
                  <td data-label="Actions">
                    <button type="button" className="copy-btn"
                      onClick={() => onCopy(r.email, r.password)}
                      title="Copy email:password" aria-label="Copy credentials">
                      <Copy size={14} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Result count */}
      {(searchQ || verif !== 'all') && filtered.length > 0 && (
        <div className="results-match-count">
          Showing {filtered.length} of {results.length}
        </div>
      )}
    </Card>
  )
}

/* ------------------------------------------------------------------ */
/*  LogTerminal                                                        */
/* ------------------------------------------------------------------ */

function LogTerminal({ logs }) {
  return (
    <Card className="log-layout">
      <div className="log-toolbar">
        <div className="log-toolbar-group">
          <Badge tone={logs.live ? 'success' : 'muted'}>
            <Radio size={12} />{logs.live ? 'Streaming' : 'Connecting'}
          </Badge>
          <span>{logs.lines.length} {logs.lines.length === 1 ? 'line' : 'lines'}</span>
        </div>
        <div className="log-toolbar-group">
          <label className="log-follow">
            <input type="checkbox" checked={logs.follow}
              onChange={(e) => logs.setFollow(e.target.checked)} />
            Auto-scroll
          </label>
          <Button size="sm" onClick={() => logs.setLines([])}>
            <Trash2 size={14} /> Clear
          </Button>
        </div>
      </div>
      <div className="log-terminal" ref={logs.boxRef}>
        {logs.lines.length === 0 ? (
          <div className="ui-empty-state">
            <ScrollText size={26} />
            <strong>No logs yet</strong>
            <p>Start a run and every checker step streams here.</p>
          </div>
        ) : (
          logs.lines.map((line, i) => (
            <div key={i} className={`log-line ${logTone(line)}`}>{line}</div>
          ))
        )}
      </div>
    </Card>
  )
}
