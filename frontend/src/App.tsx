import { useEffect, useState } from 'react'

type Project = {
  id: number
  name: string
  base_url: string
  created_at: string
  updated_at: string
}

type Scan = {
  id: number
  project_id: number | null
  url: string
  mode: string
  headless: boolean
  status: string
  started_at: string
  finished_at: string | null
  pages_tested: number
  broken_links: number
  js_errors: number
  api_failures: number
  resource_failures: number
  third_party_failures: number
  navigation_failures: number
  missing_elements: number
  slow_pages: number
  total_findings: number
  site_score: number
  risk_level: string
  unique_findings: number
  phase2_summary: string | null
  executive_summary: string | null
}

type ScanDetail = Scan & {
  raw_report: string | null
  findings: { id: number; category: string; message: string; url: string | null; note: string | null; created_at: string }[]
  artifacts: { id: number; kind: string; path: string; created_at: string }[]
  score_breakdown: { label: string; score: number; deductions: string[] }[]
  phase2_summary: string | null
  comparison: {
    previous_scan_id: number | null
    previous_score: number | null
    score_delta: number | null
    previous_risk_level: string | null
    comparison_note: string | null
  } | null
}

type LiveEvent =
  | { type: 'log'; message: string }
  | { type: 'done'; report?: string }
  | { type: 'error'; message: string }

const API_BASE = 'http://127.0.0.1:8000'

export default function App() {
  const [url, setUrl] = useState('')
  const [mode, setMode] = useState('browser')
  const [headless, setHeadless] = useState(false)
  const [report, setReport] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [scans, setScans] = useState<Scan[]>([])
  const [selectedScan, setSelectedScan] = useState<ScanDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [liveLogs, setLiveLogs] = useState<string[]>([])
  const [livePhase, setLivePhase] = useState('Idle')
  const [liveStartedAt, setLiveStartedAt] = useState<number | null>(null)
  const [liveTick, setLiveTick] = useState(0)
  const [scanLoading, setScanLoading] = useState(false)
  const [view, setView] = useState<{ kind: 'home' } | { kind: 'scan'; id: number } | { kind: 'project'; id: number }>({ kind: 'home' })

  const loadData = async () => {
    const [projectsRes, scansRes] = await Promise.all([
      fetch(`${API_BASE}/projects`),
      fetch(`${API_BASE}/scans`),
    ])
    setProjects(await projectsRes.json())
    setScans(await scansRes.json())
  }

  useEffect(() => {
    void loadData().catch((err) => setError(String(err)))
  }, [])

  useEffect(() => {
    const syncViewFromHash = () => {
      const scanMatch = window.location.hash.match(/^#scan\/(\d+)$/)
      const projectMatch = window.location.hash.match(/^#project\/(\d+)$/)
      if (scanMatch) {
        setView({ kind: 'scan', id: Number(scanMatch[1]) })
      } else if (projectMatch) {
        setView({ kind: 'project', id: Number(projectMatch[1]) })
      } else {
        setView({ kind: 'home' })
      }
    }
    syncViewFromHash()
    window.addEventListener('hashchange', syncViewFromHash)
    return () => window.removeEventListener('hashchange', syncViewFromHash)
  }, [])

  useEffect(() => {
    if (!loading) return undefined
    const timer = window.setInterval(() => {
      setLiveTick((value) => value + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [loading])

  const runScan = async () => {
    setLoading(true)
    setError('')
    setReport('')
    setLiveLogs([])
    setLivePhase('Starting')
    setLiveStartedAt(Date.now())
    try {
      const response = await fetch(`${API_BASE}/scan/live`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, mode, headless }),
      })
      if (!response.ok) {
        throw new Error(await response.text())
      }
      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      if (!reader) {
        throw new Error('Live scan stream unavailable')
      }
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() ?? ''
        for (const chunk of chunks) {
          const dataMatch = chunk.match(/^data:\s*(.+)$/m)
          if (!dataMatch) continue
          const payload = JSON.parse(dataMatch[1]) as LiveEvent
          if (payload.type === 'log') {
            setLiveLogs((current) => [...current, payload.message])
            setLivePhase(payload.message)
          } else if (payload.type === 'done') {
            setReport(payload.report ?? '')
            setLivePhase('Completed')
          } else if (payload.type === 'error') {
            setLivePhase('Error')
            throw new Error(payload.message)
          }
        }
      }
      await loadData()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const stopScan = async () => {
    try {
      const response = await fetch(`${API_BASE}/scan/live/stop`, {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error(await response.text())
      }
      setLivePhase('Stopping')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const liveElapsedSeconds = liveStartedAt ? Math.max(0, Math.round((Date.now() - liveStartedAt) / 1000)) : 0
  const formatElapsed = (seconds: number) => {
    if (seconds < 60) return `${seconds}s`
    const minutes = Math.floor(seconds / 60)
    if (seconds < 3600) return `${minutes}m`
    const hours = Math.floor(seconds / 3600)
    return `${hours}h`
  }
  const liveBars = [
    38, 62, 48, 84, 56, 72, 44, 90, 58, 76, 52, 68,
  ].map((height, index) => ({
    height: `${Math.min(100, height + ((liveTick + index) % 5) * 4)}%`,
    delay: `${index * 90}ms`,
  }))

  const openScan = (id: number) => {
    window.location.hash = `scan/${id}`
  }

  const openProject = (id: number) => {
    window.location.hash = `project/${id}`
  }

  const closeScanDetail = () => {
    setSelectedScan(null)
    window.location.hash = ''
  }

  const closeProjectDetail = () => {
    window.location.hash = ''
  }

  const openScanDetail = async (id: number) => {
    setScanLoading(true)
    try {
      const response = await fetch(`${API_BASE}/scans/${id}`)
      if (!response.ok) {
        throw new Error(await response.text())
      }
      setSelectedScan(await response.json())
    } catch (err) {
      setError(err instanceof Error ? `Failed to load scan ${id}: ${err.message}` : `Failed to load scan ${id}`)
    } finally {
      setScanLoading(false)
    }
  }

  const showDashboard = view.kind === 'home'
  const totalFindings = scans.reduce((total, scan) => total + scan.total_findings, 0)
  const selectedProject = view.kind === 'project' ? projects.find((project) => project.id === view.id) ?? null : null
  const projectScans = selectedProject ? scans.filter((scan) => scan.project_id === selectedProject.id) : []
  const latestScan = scans[0] ?? null
  const passingScans = scans.filter((scan) => scan.site_score >= 90).length
  const warningScans = scans.filter((scan) => scan.site_score < 90 && scan.site_score >= 70).length
  const needsAttentionScans = scans.filter((scan) => scan.site_score < 70).length
  const averageScore = scans.length ? Math.round(scans.reduce((total, scan) => total + scan.site_score, 0) / scans.length) : 0
  const groupedScanFindings = selectedScan
    ? Object.entries(
        selectedScan.findings.reduce<Record<string, typeof selectedScan.findings>>((acc, finding) => {
          const key = finding.category || 'uncategorized'
          if (!acc[key]) acc[key] = []
          acc[key].push(finding)
          return acc
        }, {}),
      ).sort((a, b) => b[1].length - a[1].length)
    : []

  useEffect(() => {
    if (view.kind !== 'scan') return
    setSelectedScan(null)
    if (selectedScan?.id === view.id) return
    void openScanDetail(view.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  const shellClassName = view.kind === 'home' ? 'shell shell-home' : 'shell shell-detail'

  return (
    <div className={shellClassName}>
      {showDashboard ? (
        <div className="home-layout">
          <aside className="home-sidebar">
            <div className="sidebar-brand">
              <p className="eyebrow">Autonomous QA</p>
              <h2>Control Room</h2>
              <p>Live navigation and system context.</p>
            </div>
            <div className="sidebar-status">
              <div className="sidebar-status-top">
                <span>Backend</span>
                <span className={`status-pill ${loading ? 'status-pill-active' : 'status-pill-idle'}`}>
                  {loading ? 'Live' : 'Ready'}
                </span>
              </div>
              <strong>{loading ? 'Scan in progress' : 'Connected backend'}</strong>
              <small>{API_BASE}</small>
            </div>
            <nav className="sidebar-nav">
              <button type="button" className="sidebar-link sidebar-link-active">Overview</button>
              <button type="button" className="sidebar-link" onClick={() => openProject(projects[0]?.id ?? 0)} disabled={!projects.length}>
                Project
              </button>
              <button type="button" className="sidebar-link" onClick={() => openScan(scans[0]?.id ?? 0)} disabled={!scans.length}>
                Scan
              </button>
            </nav>
            <div className="sidebar-stats">
              <div>
                <span>Projects</span>
                <strong>{projects.length}</strong>
              </div>
              <div>
                <span>Scans</span>
                <strong>{scans.length}</strong>
              </div>
              <div>
                <span>Findings</span>
                <strong>{totalFindings}</strong>
              </div>
            </div>
            <div className="sidebar-status sidebar-status-muted">
              <span className="sidebar-status-label">Live summary</span>
              <strong>{loading ? 'Scan running and collecting events' : 'System online and idle'}</strong>
              <small>
                {liveLogs.length} updates tracked
                {liveStartedAt ? ` · started ${formatElapsed(liveElapsedSeconds)} ago` : ''}
              </small>
            </div>
          </aside>

          <main className="home-content">
          <section className="overview-strip">
            <article className="overview-card overview-card-accent">
              <span>Latest score</span>
              <strong>{latestScan ? `${latestScan.site_score}/100` : 'N/A'}</strong>
              <small>{latestScan ? latestScan.url : 'Run a scan to populate the dashboard'}</small>
            </article>
            <article className="overview-card">
              <span>Average score</span>
              <strong>{scans.length ? `${averageScore}/100` : 'N/A'}</strong>
              <small>{scans.length ? `${scans.length} scans tracked` : 'No scans yet'}</small>
            </article>
            <article className="overview-card">
              <span>Passing scans</span>
              <strong>{passingScans}</strong>
              <small>{warningScans} warnings, {needsAttentionScans} need attention</small>
            </article>
            <article className="overview-card">
              <span>Projects</span>
              <strong>{projects.length}</strong>
              <small>{scans.length ? `${scans.length} scans total` : 'No scan history yet'}</small>
            </article>
          </section>

          <section className="dashboard-intro">
            <article className="intro-panel intro-panel-primary">
              <span>Operating model</span>
              <strong>Run scans, watch the health signal, and inspect evidence from one place.</strong>
              <p>Use the scan runner first, then open any project or scan row to move from overview to detail in one click.</p>
            </article>
            <article className="intro-panel">
              <span>Reading order</span>
              <strong>Signals up top, evidence below.</strong>
              <p>The dashboard emphasizes score, risk, and deltas before you reach the raw findings and artifacts.</p>
            </article>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>Run Scan</h2>
            </div>
            <div className="form-grid">
              <label>
                URL
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="Enter a URL to scan"
                />
              </label>
              <label>
                Mode
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="auto">auto</option>
                  <option value="browser">browser</option>
                  <option value="browser-fast">browser-fast</option>
                  <option value="http">http</option>
                </select>
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={headless} onChange={(e) => setHeadless(e.target.checked)} />
                Headless
              </label>
              <div className="action-buttons">
                <button onClick={runScan} disabled={loading}>
                  {loading ? 'Running...' : 'Start Scan'}
                </button>
                {loading ? (
                  <button className="secondary" onClick={stopScan}>
                    Stop Scan
                  </button>
                ) : null}
              </div>
            </div>
            {error ? <p className="error">{error}</p> : null}
            {liveLogs.length ? (
              <div className="live-log-panel">
                <div className="panel-header compact">
                  <h3>Live activity</h3>
                  <span>{liveLogs.length} updates</span>
                </div>
                <ul className="live-log-list">
                  {liveLogs.map((line, index) => (
                    <li key={`${index}-${line}`} className="live-log-item">
                      <span className="live-dot" />
                      <span>{line}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {report ? <pre className="report">{report}</pre> : null}
          </section>

          <section className="home-grid">
            <div className="panel fixed-panel">
              <div className="panel-header">
                <h2>Projects</h2>
                <span>{projects.length}</span>
              </div>
              <ul className="list scroll-list">
                {projects.map((project) => (
                  <li key={project.id} onClick={() => openProject(project.id)} className="clickable">
                    <div className="list-row-top">
                      <strong>{project.name}</strong>
                      <span className="list-badge">Project</span>
                    </div>
                    <span>{project.base_url}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="panel fixed-panel">
              <div className="panel-header">
                <h2>Scans</h2>
                <span>{scans.length}</span>
              </div>
              <ul className="list scroll-list">
                {scans.map((scan) => (
                  <li key={scan.id} onClick={() => openScan(scan.id)} className="clickable">
                    <div className="list-row-top">
                      <strong>{scan.url}</strong>
                      <span className={`list-badge list-badge-score ${scan.site_score >= 90 ? 'list-badge-good' : scan.site_score >= 70 ? 'list-badge-warn' : 'list-badge-alert'}`}>
                        {scan.site_score}/100
                      </span>
                    </div>
                    <span>{scan.status} · {scan.pages_tested} pages · {scan.total_findings} findings · unique {scan.unique_findings}</span>
                  </li>
                ))}
              </ul>
            </div>
          </section>
          </main>
        </div>
      ) : null}

      {view.kind === 'scan' ? (
        <>
          <header className="page-hero">
            <div>
              <p className="eyebrow">Scan detail</p>
              <h1>{selectedScan ? `Scan #${selectedScan.id}` : `Scan #${view.id}`}</h1>
              <p className="page-subtitle">{selectedScan?.url ?? 'Loading scan details...'}</p>
              {selectedScan ? (
                <div className="summary-pills">
                  <span className="summary-pill summary-pill-score">Score {selectedScan.site_score}/100</span>
                  <span className="summary-pill">{selectedScan.risk_level}</span>
                  <span className="summary-pill">{selectedScan.pages_tested} pages</span>
                  <span className="summary-pill">{selectedScan.unique_findings} unique findings</span>
                </div>
              ) : null}
            </div>
            <div className="page-actions">
              <button className="secondary" onClick={closeScanDetail}>Back to dashboard</button>
            </div>
          </header>
          <section className="scan-hero-band">
            <article className="band-card band-card-score">
              <span>Overall score</span>
              <strong>{selectedScan ? `${selectedScan.site_score}/100` : 'Loading'}</strong>
              <small>{selectedScan?.phase2_summary ?? 'Scoring and summarizing the scan output'}</small>
            </article>
            <article className="band-card">
              <span>Risk level</span>
              <strong>{selectedScan?.risk_level ?? 'Loading'}</strong>
              <small>{selectedScan?.comparison?.comparison_note ?? 'Comparison updates after the scan loads'}</small>
            </article>
            <article className="band-card">
              <span>Findings</span>
              <strong>{selectedScan ? selectedScan.total_findings : 0}</strong>
              <small>{selectedScan ? `${selectedScan.unique_findings} unique issues after dedupe` : 'Waiting for scan data'}</small>
            </article>
          </section>
          <section className="detail-layout">
            <aside className="detail-sidebar">
              <article className="scan-card scan-card-accent">
                <div className="scan-card-head">
                  <h3>Snapshot</h3>
                  <span>{selectedScan?.mode ?? 'Loading'}</span>
                </div>
                {selectedScan ? (
                  <div className="scan-card-list">
                    <div><span>Status</span><strong>{selectedScan.status}</strong></div>
                    <div><span>Pages tested</span><strong>{selectedScan.pages_tested}</strong></div>
                    <div><span>Total findings</span><strong>{selectedScan.total_findings}</strong></div>
                    <div><span>Site score</span><strong>{selectedScan.site_score}/100</strong></div>
                    <div><span>Risk level</span><strong>{selectedScan.risk_level}</strong></div>
                    <div><span>Unique findings</span><strong>{selectedScan.unique_findings}</strong></div>
                    <div><span>Phase 2 summary</span><strong>{selectedScan.phase2_summary ?? 'N/A'}</strong></div>
                    <div><span>Executive summary</span><strong>{selectedScan.executive_summary ?? 'N/A'}</strong></div>
                    <div><span>Broken links</span><strong>{selectedScan.broken_links}</strong></div>
                    <div><span>Started</span><strong>{selectedScan.started_at}</strong></div>
                    <div><span>Finished</span><strong>{selectedScan.finished_at ?? 'Running'}</strong></div>
                    <div><span>Headless</span><strong>{selectedScan.headless ? 'Yes' : 'No'}</strong></div>
                  </div>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading scan details...' : 'Scan details unavailable.'}</p>
                )}
              </article>
            </aside>

            <div className="detail-main">
              <div className="scan-detail-hero">
                <div>
                  <p className="eyebrow">Scan intelligence</p>
                  <p className="scan-detail-subtitle">
                    A focused view of what happened during the scan, what was found, and what artifacts were produced.
                  </p>
                </div>
                {selectedScan ? (
                  <div className="hero-metrics">
                    <div>
                      <span>Overall</span>
                      <strong>{selectedScan.site_score}/100</strong>
                    </div>
                    <div>
                      <span>Risk</span>
                      <strong>{selectedScan.risk_level}</strong>
                    </div>
                    <div>
                      <span>Findings</span>
                      <strong>{selectedScan.total_findings}</strong>
                    </div>
                  </div>
                ) : null}
              </div>

              <section className="scan-card">
                <div className="scan-card-head">
                  <h3>Findings</h3>
                <span>{selectedScan?.findings.length ?? 0}</span>
              </div>
                {selectedScan && groupedScanFindings.length ? (
                  <div className="finding-groups">
                    {groupedScanFindings.map(([category, findings]) => (
                      <details key={category} className="finding-group" open={findings.length <= 3}>
                        <summary>
                          <strong>{category}</strong>
                          <span>{findings.length}</span>
                        </summary>
                        <ul className="finding-preview-list">
                          {findings.slice(0, 3).map((finding) => (
                            <li key={finding.id}>
                              <strong>{finding.message}</strong>
                              <small>
                                {finding.url ?? 'Global'}
                                {finding.note ? ` · ${finding.note}` : ''}
                              </small>
                            </li>
                          ))}
                          {findings.length > 3 ? (
                            <li className="finding-more">
                              +{findings.length - 3} more in this category
                            </li>
                          ) : null}
                        </ul>
                      </details>
                    ))}
                  </div>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading findings...' : 'No findings captured. This scan looks clean.'}</p>
                )}
              </section>

              <section className="scan-card">
                <div className="scan-card-head">
                  <h3>Score Breakdown</h3>
                <span>{selectedScan?.site_score ?? 0}/100</span>
              </div>
                {selectedScan && selectedScan.score_breakdown.length ? (
                  <div className="finding-groups">
                    {selectedScan.score_breakdown.map((group) => (
                      <details key={group.label} className="finding-group" open>
                        <summary>
                          <strong>{group.label}</strong>
                          <span>{group.score}/100</span>
                        </summary>
                        <ul className="finding-preview-list">
                          {group.deductions.length ? (
                            group.deductions.map((deduction, index) => (
                              <li key={`${group.label}-${index}`}>
                                <strong>{deduction}</strong>
                                <small>Score impact</small>
                              </li>
                            ))
                          ) : (
                            <li className="finding-more">No deductions for this category</li>
                          )}
                        </ul>
                      </details>
                    ))}
                  </div>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading score breakdown...' : 'No score breakdown available.'}</p>
                )}
              </section>

              <section className="scan-card">
                <div className="scan-card-head">
                  <h3>Executive Summary</h3>
                  <span>At a glance</span>
                </div>
                {selectedScan?.executive_summary ? (
                  <pre className="report compact-report">{selectedScan.executive_summary}</pre>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading executive summary...' : 'No executive summary available.'}</p>
                )}
              </section>

              <section className="scan-card">
                <div className="scan-card-head">
                  <h3>Comparison</h3>
                  <span>{selectedScan?.comparison?.comparison_note ?? 'No previous scan'}</span>
                </div>
                {selectedScan?.comparison ? (
                  <div className="scan-card-list">
                    <div><span>Previous scan</span><strong>{selectedScan.comparison.previous_scan_id ?? 'N/A'}</strong></div>
                    <div><span>Previous score</span><strong>{selectedScan.comparison.previous_score ?? 'N/A'}</strong></div>
                    <div><span>Score delta</span><strong>{selectedScan.comparison.score_delta ?? 'N/A'}</strong></div>
                    <div><span>Previous risk</span><strong>{selectedScan.comparison.previous_risk_level ?? 'N/A'}</strong></div>
                  </div>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading comparison...' : 'No prior scan available for comparison.'}</p>
                )}
              </section>

              <div className="detail-grid-two">
                <article className="scan-card">
                  <div className="scan-card-head">
                    <h3>Artifacts</h3>
                    <span>{selectedScan?.artifacts.length ?? 0}</span>
                  </div>
                  {selectedScan?.artifacts.length ? (
                    <ul className="artifact-list">
                      {selectedScan.artifacts.map((artifact) => (
                        <li key={artifact.id}>
                          <strong>{artifact.kind}</strong>
                          <span>{artifact.path}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="scan-empty">{scanLoading ? 'Loading artifacts...' : 'No artifacts were saved for this scan.'}</p>
                  )}
                </article>
              </div>

              <section className="scan-raw scan-raw-spaced">
                <div className="scan-card-head">
                  <h3>Raw Report</h3>
                  <span>Full output</span>
                </div>
                {selectedScan?.raw_report ? (
                  <pre className="report compact-report">{selectedScan.raw_report}</pre>
                ) : (
                  <p className="scan-empty">{scanLoading ? 'Loading raw report...' : 'No raw report was stored for this scan.'}</p>
                )}
              </section>
            </div>
          </section>
        </>
      ) : null}

      {view.kind === 'project' && selectedProject ? (
        <>
          <header className="page-hero">
            <div>
              <p className="eyebrow">Project detail</p>
              <h1>{selectedProject.name}</h1>
              <p className="page-subtitle">{selectedProject.base_url}</p>
            </div>
            <button className="secondary" onClick={closeProjectDetail}>Back to dashboard</button>
          </header>
          <section className="detail-layout">
            <aside className="detail-sidebar">
              <article className="scan-card scan-card-accent">
                <div className="scan-card-head">
                  <h3>Snapshot</h3>
                  <span>Project</span>
                </div>
                <p>This project groups scans for the same base URL.</p>
                <div className="scan-card-list">
                  <div><span>Name</span><strong>{selectedProject.name}</strong></div>
                  <div><span>Base URL</span><strong>{selectedProject.base_url}</strong></div>
                  <div><span>Scans</span><strong>{projectScans.length}</strong></div>
                  <div><span>Created</span><strong>{selectedProject.created_at}</strong></div>
                  <div><span>Updated</span><strong>{selectedProject.updated_at}</strong></div>
                </div>
              </article>
            </aside>

            <div className="detail-main">
              <div className="scan-detail-hero">
                <div>
                  <p className="scan-detail-subtitle">
                    Dedicated project view with its base URL, update history, and the scans that belong to it.
                  </p>
                </div>
              </div>

              <div className="detail-grid-two">
                <article className="scan-card">
                  <div className="scan-card-head">
                    <h3>Recent scans</h3>
                    <span>{projectScans.length}</span>
                  </div>
                  {projectScans.length ? (
                    <ul className="timeline">
                      {projectScans.map((scan) => (
                        <li key={scan.id} className="timeline-item">
                          <div className="timeline-marker" />
                          <div>
                            <strong>{scan.status}</strong>
                            <p>{scan.pages_tested} pages · {scan.total_findings} findings</p>
                            <small>{scan.started_at}</small>
                          </div>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="scan-empty">No scans are linked to this project yet.</p>
                  )}
                </article>

                <article className="scan-card">
                  <div className="scan-card-head">
                    <h3>Linked scans</h3>
                    <span>{projectScans.length}</span>
                  </div>
                  {projectScans.length ? (
                    <ul className="artifact-list">
                      {projectScans.map((scan) => (
                        <li key={scan.id} onClick={() => openScan(scan.id)} className="clickable">
                          <strong>{scan.url}</strong>
                          <span>{scan.status} · {scan.pages_tested} pages · {scan.total_findings} findings</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="scan-empty">No linked scans available.</p>
                  )}
                </article>
              </div>
            </div>
          </section>
        </>
      ) : null}
    </div>
  )
}
