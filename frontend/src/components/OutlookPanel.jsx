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
  FileText,
  Filter,
  FolderOpen,
  Globe,
  Hash,
  Inbox,
  Layers,
  Pause,
  Play,
  RefreshCw,
  Search,
  Square,
  Target,
  Upload,
  User,
  XCircle,
} from 'lucide-react'
import { api } from '../api.js'
import { Badge, Button, Card, Input, Spinner } from './ui.jsx'

const FILTERS = [
  { id: 'all',  label: 'All'  },
  { id: 'hit',  label: 'Hits' },
  { id: 'bad',  label: 'Bad'  },
  { id: 'inbox',label: 'Inbox Found' },
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

function countLines(text) {
  return text.split('\n').filter((l) => {
    const t = l.trim()
    return t.length > 0 && !t.startsWith('#')
  }).length
}

function PasswordCell({ password, onCopy }) {
  const [shown, setShown] = useState(false)
  return (
    <span className="pw-cell">
      <button
        type="button"
        className="copy-toggle"
        onClick={() => setShown((v) => !v)}
        title={shown ? 'Hide' : 'Reveal'}
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
        title="Copy email:password"
        aria-label="Copy credentials"
      >
        <Copy size={13} />
      </button>
    </span>
  )
}

export default function OutlookPanel() {
  const [accounts, setAccounts]   = useState('')
  const [fileName, setFileName]   = useState('')
  const [proxyFile, setProxyFile] = useState('')
  const [proxyName, setProxyName] = useState('')
  const [mode, setMode]           = useState('bruter')
  const [keyword, setKeyword]     = useState('')
  const [workers, setWorkers]     = useState('')
  const [running, setRunning]     = useState(false)
  const [status, setStatus]       = useState(null)
  const [results, setResults]     = useState([])
  const [busy, setBusy]           = useState(false)
  const [toast, setToast]         = useState('')
  const [stopOpen, setStopOpen]   = useState(false)
  const [filter, setFilter]       = useState('all')
  const [sortDesc, setSortDesc]   = useState(true)
  const [configOpen, setConfigOpen] = useState(false)
  const [now, setNow]             = useState(Date.now())
  const pollRef  = useRef(null)
  const toastRef = useRef(null)
  const tickRef  = useRef(null)

  useEffect(() => {
    tickRef.current = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tickRef.current)
  }, [])

  useEffect(() => {
    if (!running) { clearInterval(pollRef.current); return undefined }
    const tick = () => {
      api.get('/api/outlook/status').then((d) => {
        setStatus(d); setResults(d.results || [])
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
    api.get('/api/outlook/status').then((d) => {
      if (d?.running) { setRunning(true); setStatus(d); setResults(d.results || []) }
      else if (d?.results?.length) { setResults(d.results); setStatus(d) }
    }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  function showToast(msg) {
    setToast(msg)
    clearTimeout(toastRef.current)
    toastRef.current = setTimeout(() => setToast(''), 2600)
  }

  async function onPickAccounts(e) {
    const f = e.target.files?.[0]; if (!f) return
    const text = await f.text()
    setAccounts(text); setFileName(f.name)
    showToast(`${f.name} — ${countLines(text)} accounts`)
  }

  async function onPickProxies(e) {
    const f = e.target.files?.[0]; if (!f) return
    const text = await f.text()
    setProxyFile(text); setProxyName(f.name)
    showToast(`${f.name} — ${countLines(text)} proxies`)
  }

  async function startJob() {
    if (busy) return
    if (mode === 'inboxer' && !keyword.trim()) {
      showToast('Enter a keyword for Inboxer mode')
      return
    }
    const list = accounts.split('\n').map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#') && l.includes(':'))
    if (!list.length) { showToast('Load an accounts.txt first'); return }
    setBusy(true)
    try {
      await api.post('/api/outlook/start', {
        accounts: list,
        mode,
        keyword: mode === 'inboxer' ? keyword.trim() : undefined,
        proxy_file: proxyFile || undefined,
        workers: workers ? Number(workers) : undefined,
      })
      setRunning(true)
      showToast(`Started — ${list.length} accounts · ${mode}`)
    } catch (e) { showToast(`Start failed: ${e.message}`) }
    finally { setBusy(false) }
  }

  async function stopJob() {
    setStopOpen(false)
    try { await api.post('/api/outlook/stop', {}); showToast('Stop requested') }
    catch (e) { showToast(`Stop failed: ${e.message}`) }
  }

  async function copyRow(email, password) {
    const txt = password ? `${email}:${password}` : email
    try { await navigator.clipboard.writeText(txt); showToast('Copied') }
    catch { showToast('Copy failed') }
  }

  async function download(kind) {
    try {
      const token = localStorage.getItem('iftools_token') || ''
      const res = await fetch(`/api/outlook/results?kind=${kind}`, {
        headers: { 'x-access-key': token },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const text = await res.text()
      const blob = new Blob([text], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = kind === 'inbox' ? 'outlook_inbox.txt'
        : kind === 'hits' ? 'outlook_hits.txt' : 'outlook_results.txt'
      a.click(); URL.revokeObjectURL(url)
    } catch (e) { showToast(`Download failed: ${e.message}`) }
  }

  // Derived
  const total    = results.length
  const target   = status?.total  || 0
  const hits     = status?.hits   || 0
  const bad      = status?.bad    || 0
  const found    = status?.found  || 0
  const retries  = status?.retries|| 0
  const done     = hits + bad
  const pct      = target ? Math.min(100, Math.round((done / target) * 100)) : 0

  const elapsedSec = (() => {
    if (!status?.started_at) return 0
    const end = status?.finished_at ? status.finished_at * 1000 : now
    return Math.max(0, (end - status.started_at * 1000) / 1000)
  })()

  const statusInfo = (() => {
    if (running) return { label: 'Running', tone: 'ok', title: 'Checking accounts' }
    if (status?.error) return { label: 'Error', tone: 'bad', title: 'Job failed' }
    if (status?.finished_at) {
      if (hits === 0 && bad > 0) return { label: 'All bad', tone: 'bad', title: 'No hits found' }
      if (target > 0 && done < target) return { label: 'Stopped', tone: 'muted', title: 'Job stopped early' }
      return { label: 'Completed', tone: 'ok', title: 'Job completed' }
    }
    return { label: 'Ready', tone: 'muted', title: 'Ready to check' }
  })()

  const filteredResults = (() => {
    let rows = results
    if (filter === 'hit')   rows = rows.filter((r) => r.status === 'hit')
    if (filter === 'bad')   rows = rows.filter((r) => r.status === 'bad')
    if (filter === 'inbox') rows = rows.filter((r) => (r.found || 0) > 0)
    if (!sortDesc) rows = [...rows].reverse()
    return rows
  })()

  return (
    <div style={styles.wrap}>

      {/* Hero */}
      <Card style={styles.hero}>
        <div style={styles.heroText}>
          <div style={styles.eyebrow}>OUTLOOK CHECKER</div>
          <h1 style={styles.heroTitle}>{statusInfo.title}</h1>
          <p style={styles.heroSub}>
            Microsoft OAuth2 bruter + inbox keyword searcher.
            Supports Outlook, Hotmail, Live &amp; MSN accounts.
            No IMAP setup required.
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
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div className="status-metrics">
          <StatBox label="Target"   value={target || '-'} icon={Target} />
          <StatBox label="Hits"     value={hits}  tone="ok"  icon={CheckCircle2} />
          <StatBox label="Bad"      value={bad}   tone="bad" icon={XCircle} />
          {mode === 'inboxer' && (
            <StatBox label="Found"  value={found} tone="ok"  icon={Inbox} />
          )}
          <StatBox label="Retries"  value={retries} icon={RefreshCw} small />
          <StatBox label="Started"  value={fmtTime(status?.started_at)} icon={Play} small />
          <StatBox
            label={running ? 'Elapsed' : 'Duration'}
            value={fmtDuration(elapsedSec)}
            icon={running ? Clock3 : Square}
            small
          />
        </div>
      </Card>

      {/* Progress */}
      {target > 0 && (
        <Card style={styles.progressCard}>
          <div style={styles.progressHead}>
            <span style={{ color: 'var(--text-3)', fontWeight: 600 }}>Progress</span>
            <span style={{ fontWeight: 700 }}>
              {done} / {target}{' '}
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
          <div style={styles.progressLegend}>
            {hits > 0 && <span style={{ color: 'var(--success-deep)' }}>● {hits} hits</span>}
            {found > 0 && <span style={{ color: 'var(--info)' }}>● {found} inbox matches</span>}
            {bad > 0  && <span style={{ color: 'var(--danger-deep)' }}>● {bad} bad</span>}
            {running && target - done > 0 && (
              <span style={{ color: 'var(--text-3)' }}>● {target - done} pending</span>
            )}
          </div>
        </Card>
      )}

      {/* Configure (collapsible) */}
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <button
          type="button"
          className="ui-card-strip config-toggle"
          onClick={() => setConfigOpen((v) => !v)}
          aria-expanded={configOpen}
        >
          <Layers size={15} />
          <strong>Configure Run</strong>
          <span className="config-toggle-chevron">
            {configOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </span>
        </button>

        {configOpen && (
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
                    {accounts
                      ? <div className="run-field-hint ok"><code>{countLines(accounts)} accounts · email:password</code></div>
                      : <div className="run-field-hint">One <code>email:password</code> per line</div>
                    }
                  </div>
                  <div className="run-field-group">
                    <div className="run-field-label">Proxies <span style={{ fontSize: 10, color: 'var(--text-3)' }}>optional</span></div>
                    <label className="run-file-btn">
                      <Upload size={14} />
                      <span>{proxyName || 'Choose proxies.txt'}</span>
                      <input type="file" accept=".txt" onChange={onPickProxies} hidden />
                    </label>
                    {proxyFile
                      ? <div className="run-field-hint ok"><code>{countLines(proxyFile)} proxies loaded</code></div>
                      : <div className="run-field-hint">Format: <code>host:port:user:pass</code> or <code>http://user:pass@host:port</code></div>
                    }
                  </div>
                </div>
              </div>
            </div>

            <div className="run-step-divider" />

            {/* Step 2: Mode + options */}
            <div className="run-step">
              <div className="run-step-num">2</div>
              <div className="run-step-body">
                <div className="run-step-title">Configure</div>
                <div className="run-step-fields">
                  <div className="run-field-group">
                    <div className="run-field-label">Mode</div>
                    <div className="glass-segmented" role="group" aria-label="Mode">
                      <button
                        type="button"
                        className={mode === 'bruter' ? 'active' : ''}
                        aria-pressed={mode === 'bruter'}
                        onClick={() => setMode('bruter')}
                      >
                        Bruter Only
                      </button>
                      <button
                        type="button"
                        className={mode === 'inboxer' ? 'active' : ''}
                        aria-pressed={mode === 'inboxer'}
                        onClick={() => setMode('inboxer')}
                      >
                        Inboxer + Bruter
                      </button>
                    </div>
                    <div className="run-field-hint">
                      {mode === 'inboxer'
                        ? 'Checks credentials AND searches inbox for keyword'
                        : 'Validates credentials only — Name + Country extracted'}
                    </div>
                  </div>

                  {mode === 'inboxer' && (
                    <div className="run-field-group">
                      <label htmlFor="ol-keyword" className="run-field-label">
                        Keyword <span className="run-field-required">required</span>
                      </label>
                      <Input
                        id="ol-keyword"
                        type="text"
                        placeholder="e.g. Amazon, PayPal, password reset"
                        value={keyword}
                        onChange={(e) => setKeyword(e.target.value)}
                        autoComplete="off"
                        name="ol-keyword-x"
                        data-lpignore="true"
                      />
                      <div className="run-field-hint">Searched across inbox + deleted items</div>
                    </div>
                  )}

                  <div className="run-field-group">
                    <label htmlFor="ol-workers" className="run-field-label">
                      Workers
                      <span className="run-field-tooltip" title="Default: 100 (bruter) / 15 (inboxer)">ⓘ</span>
                    </label>
                    <Input
                      id="ol-workers"
                      type="number"
                      min="1"
                      max="150"
                      placeholder={mode === 'inboxer' ? '15' : '100'}
                      value={workers}
                      onChange={(e) => setWorkers(e.target.value)}
                      className="status-count-input"
                      autoComplete="off"
                      name="ol-workers-x"
                      data-lpignore="true"
                    />
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
                    <Button variant="destructive" className="status-action-stop"
                      onClick={() => setStopOpen(true)}>
                      <Pause size={15} /> Stop
                    </Button>
                  ) : (
                    <Button variant="primary" className="status-action-primary"
                      onClick={startJob}
                      disabled={busy || !accounts || (mode === 'inboxer' && !keyword)}
                      title={!accounts ? 'Load accounts first' : mode === 'inboxer' && !keyword ? 'Enter keyword' : ''}>
                      {busy ? <Spinner /> : <Play size={16} />} Start checker
                    </Button>
                  )}
                  <Button className="status-action-nav" onClick={() => download('hits')}
                    disabled={!hits} title="Download valid accounts">
                    <Download size={15} /> Hits ({hits})
                  </Button>
                  {mode === 'inboxer' && (
                    <Button className="status-action-nav" onClick={() => download('inbox')}
                      disabled={!found} title="Download inbox matches only">
                      <Inbox size={15} /> Inbox ({found})
                    </Button>
                  )}
                  <Button className="status-action-nav" onClick={() => download('all')}
                    disabled={!total} title="Download all results">
                    <FileText size={15} /> All ({total})
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
            <div className="results-title"><Hash size={14} /><strong>Results</strong></div>
            <div className="results-toolbar">
              <div className="filter-tabs" role="group" aria-label="Filter">
                {FILTERS.filter((f) => f.id !== 'inbox' || mode === 'inboxer').map((f) => {
                  const count = f.id === 'all' ? total
                    : f.id === 'hit'   ? hits
                    : f.id === 'bad'   ? bad
                    : found
                  return (
                    <button key={f.id} type="button"
                      className={`filter-tab${filter === f.id ? ' active' : ''}${f.id === 'hit' ? ' tab-ok' : f.id === 'bad' ? ' tab-fail' : f.id === 'inbox' ? ' tab-check' : ''}`}
                      onClick={() => setFilter(f.id)}>
                      {f.label}
                      <span className="filter-tab-count">{count}</span>
                    </button>
                  )
                })}
              </div>
              <button type="button" className="sort-btn"
                onClick={() => setSortDesc((v) => !v)}
                title={sortDesc ? 'Newest first' : 'Oldest first'}>
                {sortDesc ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
                {sortDesc ? 'Newest' : 'Oldest'}
              </button>
            </div>
          </div>

          {filteredResults.length === 0 ? (
            <div className="ui-empty-state" style={{ minHeight: 120 }}>
              <Filter size={22} /><strong>No {filter} results</strong>
            </div>
          ) : (
            <div className="accounts-table-wrap">
              <table className="accounts-table">
                <thead>
                  <tr>
                    <th style={{ width: 40 }}>#</th>
                    <th>Email</th>
                    <th>Password</th>
                    <th>Status</th>
                    <th>Name</th>
                    <th>Country</th>
                    {mode === 'inboxer' && <th style={{ width: 80 }}>Found</th>}
                    <th style={{ width: 48 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredResults.map((r, i) => (
                    <tr key={`${r.email}-${i}`}
                      className={r.status === 'hit' ? 'row-ok' : r.status === 'bad' ? 'row-fail' : ''}>
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
                        {r.status === 'hit'
                          ? <Badge tone="success">Hit ✓</Badge>
                          : <Badge tone="danger">Bad</Badge>
                        }
                      </td>
                      <td data-label="Name">
                        <span style={styles.infoCell}>
                          {r.name ? <><User size={11} style={{ opacity: 0.5, marginRight: 4 }} />{r.name}</> : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                        </span>
                      </td>
                      <td data-label="Country">
                        <span style={styles.infoCell}>
                          {r.country ? <><Globe size={11} style={{ opacity: 0.5, marginRight: 4 }} />{r.country}</> : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                        </span>
                      </td>
                      {mode === 'inboxer' && (
                        <td data-label="Found">
                          {(r.found || 0) > 0
                            ? <Badge tone="info"><Search size={10} /> {r.found}</Badge>
                            : <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
                          }
                        </td>
                      )}
                      <td data-label="Copy">
                        <button type="button" className="copy-btn"
                          onClick={() => copyRow(r.email, r.password)}
                          title="Copy" aria-label="Copy">
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
            <Inbox size={28} />
            <strong>No results yet</strong>
            <p>Load accounts, configure, then start. Results appear here in real time.</p>
          </div>
        </Card>
      )}

      {/* Stop confirm */}
      {stopOpen && (
        <div className="ui-dialog-backdrop" onClick={() => setStopOpen(false)}>
          <div className="ui-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="ui-dialog-header">
              <h2>Stop the running job?</h2>
              <button className="ui-dialog-close" onClick={() => setStopOpen(false)}>×</button>
            </div>
            <div className="ui-dialog-body">
              Current account finishes, then the job stops. Results so far are kept.
            </div>
            <div className="ui-dialog-footer">
              <Button onClick={() => setStopOpen(false)}>Cancel</Button>
              <Button variant="destructive" onClick={stopJob}><XCircle size={15} /> Stop</Button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="toast" style={{ padding: '12px 26px', fontSize: 14 }}
          role="status" aria-live="polite">
          {toast}
        </div>
      )}
    </div>
  )
}

function StatBox({ label, value, tone, small, icon: Icon }) {
  const color = tone === 'ok' ? 'var(--success-deep)'
    : tone === 'bad' ? 'var(--danger-deep)'
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
  progressCard: { padding: '16px 20px' },
  progressHead: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 12.5, marginBottom: 10 },
  progressTrack: { height: 10, borderRadius: 4, background: 'var(--surface-2)', overflow: 'hidden' },
  progressFill: { height: '100%', borderRadius: 4, transition: 'background 0.3s', willChange: 'transform', transformOrigin: 'left center' },
  progressLegend: { display: 'flex', gap: 14, flexWrap: 'wrap', marginTop: 10, fontSize: 12, fontWeight: 600 },
  infoCell: { display: 'inline-flex', alignItems: 'center', fontSize: 12, color: 'var(--text-2)' },
}
