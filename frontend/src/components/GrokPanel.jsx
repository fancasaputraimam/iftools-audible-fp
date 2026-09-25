import React, { useEffect, useRef, useState } from 'react'
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock3,
  Copy,
  Download,
  Eye,
  EyeOff,
  Gauge,
  ListChecks,
  ListEnd,
  Pause,
  Play,
  Rocket,
  ScrollText,
  Square,
  Target,
  Trash2,
  Upload,
  XCircle,
  Zap,
} from 'lucide-react'
import { api, subscribeLogs } from '../api.js'
import { Badge, Button, Card, Dialog, Input, Spinner } from './ui.jsx'
import DownloadModal from './DownloadModal.jsx'

const SPEEDS = [
  { id: 'slow', label: 'Slow' },
  { id: 'normal', label: 'Normal' },
  { id: 'fast', label: 'Fast' },
  { id: 'maximum', label: 'Maximum' },
]

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'ok', label: 'Hits' },
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

export default function GrokPanel() {
  const [count, setCount] = useState('10')
  const [speed, setSpeed] = useState('normal')
  const [proxy, setProxy] = useState('')
  const [authMode, setAuthMode] = useState('email')
  const [accountFile, setAccountFile] = useState('')
  const [accountName, setAccountName] = useState('')
  const [workers, setWorkers] = useState('')
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState([])
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [stopOpen, setStopOpen] = useState(false)
  const [dlOpen, setDlOpen] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [filter, setFilter] = useState('all')
  const [sortDesc, setSortDesc] = useState(true)
  const [configOpen, setConfigOpen] = useState(false)
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
    if (line.includes('[+]') || line.includes('SUCCESS')) return 'success'
    if (line.includes('[-]') || line.includes('[!]') || line.includes('FAIL')) return 'danger'
    if (line.includes('[*]')) return 'accent'
    return ''
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
      api.get('/api/grok/status').then((d) => {
        setStatus(d)
        setResults(d.results || [])
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
    api.get('/api/grok/status').then((d) => {
      if (d && d.running) {
        setRunning(true); setStatus(d); setResults(d.results || [])
      } else if (d && Array.isArray(d.results) && d.results.length) {
        setResults(d.results); setStatus(d)
      }
    }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  async function onPickAccounts(e) {
    const f = e.target.files?.[0]; if (!f) return
    const text = await f.text()
    setAccountFile(text)
    setAccountName(f.name)
    showToast(`${f.name} — ${text.split('\n').filter((l) => l.trim()).length} accounts`)
  }

  async function startJob() {
    if (busy) return
    const n = Number(count)
    if (!n || n < 1) { showToast('Enter an account count'); return }
    if (authMode === 'google' && !accountFile) { showToast('Load an account file for Google SSO mode'); return }
    setBusy(true)
    try {
      await api.post('/api/grok/start', {
        count: n, speed, proxy,
        auth_mode: authMode,
        account_file: authMode === 'google' ? accountFile : undefined,
        workers: workers ? Number(workers) : undefined,
      })
      setRunning(true)
      showToast(`Started — ${n} accounts · ${speed} · ${authMode}`)
    } catch (e) { showToast(`Start failed: ${e.message}`) }
    finally { setBusy(false) }
  }

  async function stopJob() {
    setStopOpen(false)
    try { await api.post('/api/grok/stop', {}); showToast('Stop requested') }
    catch (e) { showToast(`Stop failed: ${e.message}`) }
  }

  async function copyRow(email, password) {
    const txt = password ? `${email}:${password}` : email
    try { await navigator.clipboard.writeText(txt); showToast('Copied') }
    catch { showToast('Copy failed') }
  }

  // derived
  const total = results.length
  const target = status?.total || 0
  const hitCount = status?.ok || 0
  const failCount = status?.fail || 0
  const processed = hitCount + failCount
  const pct = target ? Math.min(100, Math.round((processed / target) * 100)) : 0

  const elapsedSec = (() => {
    if (!status?.started_at) return 0
    const end = status?.finished_at ? status.finished_at * 1000 : now
    return Math.max(0, (end - status.started_at * 1000) / 1000)
  })()

  const statusInfo = (() => {
    if (running) return { label: 'Running', tone: 'ok', title: 'Registering accounts' }
    if (status?.error) return { label: 'Error', tone: 'bad', title: 'Job failed' }
    if (status?.finished_at) {
      if (hitCount === 0 && failCount > 0) return { label: 'All failed', tone: 'bad', title: 'No accounts registered' }
      if (target > 0 && processed < target) return { label: 'Stopped', tone: 'muted', title: 'Job stopped early' }
      return { label: 'Completed', tone: 'ok', title: 'Job completed' }
    }
    return { label: 'Ready', tone: 'muted', title: 'Ready to register' }
  })()

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
          <div style={styles.eyebrow}>GROK CLI FARM</div>
          <h1 style={styles.heroTitle}>{statusInfo.title}</h1>
          <p style={styles.heroSub}>
            xAI / Grok mass registration. Full-HTTP tempmail farm, local free
            Turnstile solver, device-code OAuth + optional 9router inject.
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
          <Stat label="Target" value={target || '-'} icon={Target} />
          <Stat label="Hits" value={hitCount} tone="ok" icon={CheckCircle2} />
          <Stat label="Fails" value={failCount} tone="bad" icon={XCircle} />
          <Stat label="Started" value={fmtTime(status?.started_at)} icon={Play} small />
          <Stat
            label={running ? 'Elapsed' : 'Duration'}
            value={fmtDuration(elapsedSec)}
            icon={running ? Clock3 : Square}
            small
          />
        </div>
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
          {(hitCount > 0 || failCount > 0) && (
            <div style={styles.progressLegend}>
              {hitCount > 0 && <span style={{ color: 'var(--success-deep)' }}>● {hitCount} hits</span>}
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
        <button
          type="button"
          className="ui-card-strip config-toggle"
          onClick={() => setConfigOpen((v) => !v)}
          aria-expanded={configOpen}
        >
          <ListChecks size={15} />
          <strong>Configure Run</strong>
          <span className="config-toggle-chevron">
            {configOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </span>
        </button>
        {configOpen && (
        <div className="run-steps">

          <div className="run-step">
            <div className="run-step-num">1</div>
            <div className="run-step-body">
              <div className="run-step-title">Configure</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">Accounts <span className="run-field-required">required</span></div>
                  <Input
                    type="number" min="1" max="5000" placeholder="10"
                    value={count} onChange={(e) => setCount(e.target.value)}
                    className="status-count-input" autoComplete="off"
                    name="grok-count-x" data-lpignore="true" data-1p-ignore="true"
                  />
                  <div className="run-field-hint">How many Grok accounts to register</div>
                </div>
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
                    {speed === 'maximum' ? '⚠ 7 workers — needs stable proxies'
                      : speed === 'slow' ? '1 worker, ~3–5/min'
                      : speed === 'normal' ? '3 workers, ~9–12/min (recommended)'
                      : '5 workers, ~16–20/min'}
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div className="run-step-divider" />

          <div className="run-step">
            <div className="run-step-num">2</div>
            <div className="run-step-body">
              <div className="run-step-title">Mode &amp; proxy</div>
              <div className="run-step-fields">
                <div className="run-field-group">
                  <div className="run-field-label">Auth mode</div>
                  <div className="glass-segmented" role="group" aria-label="Auth mode">
                    <button type="button" className={authMode === 'email' ? 'active' : ''}
                      aria-pressed={authMode === 'email'} onClick={() => setAuthMode('email')}>
                      Tempmail (email)
                    </button>
                    <button type="button" className={authMode === 'google' ? 'active' : ''}
                      aria-pressed={authMode === 'google'} onClick={() => setAuthMode('google')}>
                      Google SSO
                    </button>
                  </div>
                  <div className="run-field-hint">
                    {authMode === 'email'
                      ? 'Full-HTTP tempmail signup — no external accounts needed'
                      : 'Browser SSO with an email|password account file'}
                  </div>
                </div>
                {authMode === 'google' && (
                  <div className="run-field-group">
                    <div className="run-field-label">
                      Account file <span className="run-field-required">required</span>
                    </div>
                    <label className="run-file-btn">
                      <Upload size={14} />
                      <span>{accountName || 'Choose accounts.txt'}</span>
                      <input type="file" accept=".txt" onChange={onPickAccounts} hidden />
                    </label>
                    <div className="run-field-hint">
                      One <code>email|password</code> or <code>email:password</code> per line
                    </div>
                  </div>
                )}
                <div className="run-field-group">
                  <label htmlFor="grok-proxy" className="run-field-label">
                    Proxy <span style={{ fontSize: 10, color: 'var(--text-3)' }}>optional</span>
                  </label>
                  <Input
                    id="grok-proxy" type="text" placeholder="http://user:pass@host:port"
                    value={proxy} onChange={(e) => setProxy(e.target.value)}
                    autoComplete="off" name="grok-proxy-x" data-lpignore="true" data-1p-ignore="true"
                  />
                  <div className="run-field-hint">
                    Single proxy, or leave blank to use the VPS IP directly
                  </div>
                </div>
                <div className="run-field-group">
                  <label htmlFor="grok-workers" className="run-field-label">
                    Workers
                    <span className="run-field-tooltip" title="1 = most accurate">ⓘ</span>
                  </label>
                  <Input id="grok-workers" type="number" min="1" max="12" placeholder="Auto"
                    value={workers} onChange={(e) => setWorkers(e.target.value)}
                    className="status-count-input" autoComplete="off"
                    name="grok-workers-x" data-lpignore="true" data-1p-ignore="true" />
                  <div className="run-field-hint">Overrides the speed profile</div>
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
                    disabled={busy || !count || (authMode === 'google' && !accountFile)}
                    title={authMode === 'google' && !accountFile ? 'Load an account file first' : ''}>
                    {busy ? <Spinner /> : <Play size={16} />} Start farm
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

      {/* Results table */}
      {total > 0 ? (
        <Card style={{ padding: 0, overflow: 'hidden' }}>
          <div className="results-header">
            <div className="results-title"><Rocket size={15} /><strong>Results</strong></div>
            <div className="results-toolbar">
              <div className="filter-tabs" role="group" aria-label="Filter results">
                {FILTERS.map((f) => {
                  const c = f.id === 'all' ? total : f.id === 'ok' ? hitCount : failCount
                  return (
                    <button key={f.id} type="button"
                      className={`filter-tab${filter === f.id ? ' active' : ''}${f.id === 'ok' ? ' tab-ok' : f.id === 'fail' ? ' tab-fail' : ''}`}
                      onClick={() => setFilter(f.id)}>
                      {f.label}
                      <span className="filter-tab-count">{c}</span>
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
                    <tr key={`${r.email}-${i}`} className={r.status === 'ok' ? 'row-ok' : r.status === 'fail' ? 'row-fail' : ''}>
                      <td data-label="#">{i + 1}</td>
                      <td data-label="Email">
                        <code style={{ fontSize: 12 }}>{r.email}</code>
                      </td>
                      <td data-label="Password">
                        <PasswordCell password={r.password} onCopy={() => copyRow(r.email, r.password)} />
                      </td>
                      <td data-label="Status">
                        {r.status === 'ok'
                          ? <Badge tone="success">OK ✓</Badge>
                          : r.status === 'fail'
                          ? <Badge tone="danger">Fail</Badge>
                          : <Badge tone="warning">Check</Badge>}
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
            <Zap size={28} />
            <strong>No accounts yet</strong>
            <p>Set a count, pick a speed, then start the farm.</p>
          </div>
        </Card>
      )}

      {/* Log stream */}
      <Card className="log-layout" style={{ maxWidth: '100%' }}>
        <div className="log-toolbar">
          <div className="log-toolbar-group">
            <Badge tone={logLive ? 'success' : 'muted'}>
              <ScrollText size={12} />{logLive ? 'Streaming' : 'Connecting'}
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
              <p>Start a run and every farm step streams here.</p>
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
        In-flight registrations finish, then the job stops. Results so far are kept.
      </Dialog>

      <DownloadModal
        open={dlOpen}
        onClose={() => setDlOpen(false)}
        prefix="grok"
        showToast={showToast}
        options={[
          { id: 'hits', label: 'Registered accounts', hint: 'Accounts with a token', count: hitCount, kind: 'hits', tone: 'success' },
          { id: 'fail', label: 'Failed', hint: 'No token — captcha block, OTP fail, etc.', count: failCount, kind: 'fail', tone: 'danger' },
        ]}
        cols={[
          { id: 'all', label: 'Everything', hint: 'All accounts', kinds: ['all'] },
        ]}
      />

      {toast && (
        <div className="toast" style={{ padding: '12px 26px', fontSize: 14 }} role="status" aria-live="polite">
          {toast}
        </div>
      )}
    </div>
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
      <div className={small ? 'status-stat-value is-small' : 'status-stat-value'} style={{ color }}>
        {value}
      </div>
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
  progressCard: { padding: '16px 20px' },
  progressHead: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 12.5, marginBottom: 10, flexWrap: 'wrap', gap: 8 },
  progressTrack: { height: 10, borderRadius: 4, background: 'var(--surface-2)', overflow: 'hidden' },
  progressFill: { height: '100%', borderRadius: 4, transition: 'background 0.3s', willChange: 'transform', transformOrigin: 'left center' },
  progressLegend: { display: 'flex', gap: 14, flexWrap: 'wrap', marginTop: 10, fontSize: 12, fontWeight: 600 },
}
