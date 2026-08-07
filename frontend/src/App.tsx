import { useEffect, useRef, useState } from 'react'

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
  | { type: 'done'; report?: string; scan_id?: number }
  | { type: 'stopped'; message: string; report?: string; scan_id?: number }
  | { type: 'error'; message: string }

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'
const TOKEN_KEY = 'qa_access_token'

const classifyLiveLog = (message: string) => {
  const normalized = message.toLowerCase()
  if (normalized.includes('error') || normalized.includes('failed') || normalized.includes('broken')) return { kind: 'error', label: 'Issue', icon: '!' }
  if (normalized.includes('complete') || normalized.includes('ready') || normalized.includes('finished')) return { kind: 'success', label: 'Done', icon: '✓' }
  if (normalized.includes('queue') || normalized.includes('discover')) return { kind: 'discovery', label: 'Discover', icon: '◇' }
  if (normalized.includes('click') || normalized.includes('scroll') || normalized.includes('action')) return { kind: 'action', label: 'Action', icon: '↗' }
  if (normalized.includes('page') || normalized.includes('visit') || normalized.includes('fetch')) return { kind: 'navigation', label: 'Navigate', icon: '◎' }
  return { kind: 'info', label: 'Info', icon: '·' }
}

type CompletedScoreGroup = { label: string; score: number; deductions: string[] }
type CompletedFinding = { severity: string; message: string }
type Toast = { id: number; kind: 'success' | 'error' | 'info'; message: string }

const parseCompletedReport = (raw: string) => {
  const lines = raw.split(/\r?\n/)
  const exactValue = (label: string) => lines.find((line) => line.startsWith(`${label}:`))?.slice(label.length + 1).trim() ?? ''
  const exactNumber = (label: string) => Number.parseInt(exactValue(label), 10) || 0
  const section = (heading: string, stops: string[]) => {
    const start = lines.indexOf(heading)
    if (start < 0) return []
    const output: string[] = []
    for (let index = start + 1; index < lines.length; index += 1) {
      if (stops.some((stop) => lines[index].startsWith(stop))) break
      output.push(lines[index])
    }
    return output
  }

  const scoreLines = section('Score Breakdown:', ['Overall Score Formula:', 'Executive Summary:', 'Site Score:'])
  const scoreBreakdown: CompletedScoreGroup[] = []
  for (const line of scoreLines) {
    const scoreMatch = line.match(/^- (.+):\s*(\d+)\/100$/)
    if (scoreMatch) {
      scoreBreakdown.push({ label: scoreMatch[1], score: Number(scoreMatch[2]), deductions: [] })
    } else if (line.trim() && scoreBreakdown.length) {
      scoreBreakdown[scoreBreakdown.length - 1].deductions.push(line.trim().replace(/^-\s*/, ''))
    }
  }

  const testedPages = section('Tested Pages:', ['Clicked Links:', 'Page Summaries:', 'Crawl Graph:', 'Phase 2 Summary:'])
    .filter((line) => line.startsWith('- '))
    .map((line) => line.slice(2).trim())
  const executiveSummary = section('Executive Summary:', ['Site Score:'])
    .filter((line) => line.startsWith('- '))
    .map((line) => line.slice(2).trim())
  const findings: CompletedFinding[] = section('Findings:', [])
    .filter((line) => line.startsWith('- ['))
    .map((line) => {
      const match = line.match(/^- \[([^\]]+)\]\s*(.*)$/)
      return { severity: match?.[1] ?? 'Info', message: match?.[2] ?? line.slice(2) }
    })

  return {
    targetUrl: exactValue('Target URL'),
    pagesTested: exactNumber('Pages Tested'),
    siteScore: Number.parseInt(exactValue('Site Score'), 10) || 0,
    riskLevel: exactValue('Risk Level') || 'Unknown',
    totalFindings: exactNumber('Total Findings'),
    uniqueFindings: exactNumber('Unique Findings'),
    brokenLinks: exactNumber('Broken Links'),
    jsErrors: exactNumber('JS Errors'),
    apiFailures: exactNumber('API Failures'),
    resourceFailures: exactNumber('Resource Failures'),
    navigationFailures: exactNumber('Navigation Failures'),
    slowPages: exactNumber('Slow Pages Unique'),
    testedPages,
    executiveSummary,
    scoreBreakdown,
    findings,
  }
}

const formatProjectDate = (value: string) => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

export default function App() {
  const [token, setToken] = useState(() => window.localStorage.getItem(TOKEN_KEY) ?? '')
  const [authMode, setAuthMode] = useState<'login' | 'register' | 'forgot' | 'reset'>('login')
  const [authEmail, setAuthEmail] = useState('')
  const [authPassword, setAuthPassword] = useState('')
  const [authConfirmPassword, setAuthConfirmPassword] = useState('')
  const [authResetToken, setAuthResetToken] = useState('')
  const [authBusy, setAuthBusy] = useState(false)
  const [googleBusy, setGoogleBusy] = useState(false)
  const [authError, setAuthError] = useState('')
  const [authNotice, setAuthNotice] = useState('')
  const [toast, setToast] = useState<Toast | null>(null)
  const [url, setUrl] = useState('')
  const [mode, setMode] = useState('browser')
  const [headless, setHeadless] = useState(false)
  const [report, setReport] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [scans, setScans] = useState<Scan[]>([])
  const [projectQuery, setProjectQuery] = useState('')
  const [scanQuery, setScanQuery] = useState('')
  const [scanStatusFilter, setScanStatusFilter] = useState('all')
  const [projectPage, setProjectPage] = useState(1)
  const [scanPage, setScanPage] = useState(1)
  const [projectHistoryPage, setProjectHistoryPage] = useState(1)
  const [selectedScan, setSelectedScan] = useState<ScanDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [liveLogs, setLiveLogs] = useState<string[]>([])
  const liveLogRef = useRef<HTMLUListElement | null>(null)
  const [livePhase, setLivePhase] = useState('Idle')
  const [liveOutcome, setLiveOutcome] = useState<'idle' | 'running' | 'completed' | 'stopped' | 'error'>('idle')
  const [liveStartedAt, setLiveStartedAt] = useState<number | null>(null)
  const [liveTick, setLiveTick] = useState(0)
  const [scanLoading, setScanLoading] = useState(false)
  const [view, setView] = useState<{ kind: 'home' } | { kind: 'scan'; id: number } | { kind: 'project'; id: number }>({ kind: 'home' })

  const showToast = (message: string, kind: Toast['kind'] = 'info') => {
    const id = Date.now()
    setToast({ id, kind, message })
    window.setTimeout(() => {
      setToast((current) => current?.id === id ? null : current)
    }, 3200)
  }

  const setAuthRoute = (mode: 'login' | 'register' | 'forgot' | 'reset', replace = false) => {
    const paths = { login: '/login', register: '/register', forgot: '/forgot-password', reset: '/reset-password' }
    setAuthMode(mode)
    window.history[replace ? 'replaceState' : 'pushState']({}, '', paths[mode])
  }

  useEffect(() => {
    const pathMode = { '/login': 'login', '/register': 'register', '/forgot-password': 'forgot', '/reset-password': 'reset' }[window.location.pathname] as 'login' | 'register' | 'forgot' | 'reset' | undefined
    if (pathMode) setAuthMode(pathMode)
    else if (!token && !window.location.hash) window.history.replaceState({}, '', '/login')
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    const googleToken = params.get('oauth_token')
    if (googleToken) {
      setToken(googleToken)
      window.localStorage.setItem(TOKEN_KEY, googleToken)
      showToast('Signed in successfully', 'success')
      window.history.replaceState({}, '', '/')
      return
    }
    if (params.get('verified') === '1') {
      setAuthNotice('Email verified successfully. You can now sign in.')
      window.history.replaceState({}, '', '/login')
    }
    const resetToken = params.get('reset_token')
    const resetEmail = params.get('reset_email')
    if (resetToken && resetEmail) {
      setAuthRoute('reset', true)
      setAuthResetToken(resetToken)
      setAuthEmail(resetEmail)
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  useEffect(() => {
    if (!token) return
    const authPaths = ['/login', '/register', '/forgot-password', '/reset-password']
    if (!authPaths.includes(window.location.pathname)) return
    const authenticatedHash = /^#(?:scan|project)\/\d+$/.test(window.location.hash) ? window.location.hash : ''
    window.history.replaceState({}, '', `/${authenticatedHash}`)
  }, [token])

  useEffect(() => {
    const handlePopState = () => {
      const modes = { '/login': 'login', '/register': 'register', '/forgot-password': 'forgot', '/reset-password': 'reset' } as const
      const mode = modes[window.location.pathname as keyof typeof modes]
      if (mode) setAuthMode(mode)
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  const signInWithGoogle = async () => {
    setGoogleBusy(true)
    setAuthError('')
    try {
      const response = await fetch(`${API_BASE}/auth/google`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? 'Google sign-in is unavailable')
      window.location.href = payload.authorization_url
    } catch (err) {
      setGoogleBusy(false)
      setAuthError(err instanceof Error ? err.message : String(err))
    }
  }

  const apiFetch = (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return fetch(`${API_BASE}${path}`, { ...init, headers })
  }

  const loadData = async () => {
    if (!token) return
    const [projectsRes, scansRes] = await Promise.all([
      apiFetch('/projects'),
      apiFetch('/scans'),
    ])
    if (projectsRes.status === 401 || scansRes.status === 401) {
      setToken('')
      window.localStorage.removeItem(TOKEN_KEY)
      setAuthMode('login')
      window.history.replaceState({}, '', '/login')
      showToast('Your session expired. Please sign in again.', 'error')
      throw new Error('Your session has expired. Please log in again.')
    }
    if (!projectsRes.ok || !scansRes.ok) throw new Error('Unable to load dashboard data')
    setProjects(await projectsRes.json())
    setScans(await scansRes.json())
  }

  const submitAuth = async (event: React.FormEvent) => {
    event.preventDefault()
    setAuthBusy(true)
    setAuthError('')
    if ((authMode === 'register' || authMode === 'reset') && authPassword !== authConfirmPassword) {
      const message = 'Passwords do not match'
      setAuthError(message)
      showToast(message, 'error')
      setAuthBusy(false)
      return
    }
    try {
      const endpoint = authMode === 'forgot' ? 'forgot-password' : authMode === 'reset' ? 'reset-password' : authMode
      const body = authMode === 'forgot' ? { email: authEmail } : authMode === 'reset' ? { email: authEmail, token: authResetToken, password: authPassword } : { email: authEmail, password: authPassword }
      const response = await fetch(API_BASE + '/auth/' + endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? 'Authentication failed')
      if (authMode === 'register') {
        const message = payload.message ?? 'Verification email sent. Check your inbox.'
        setAuthNotice(message)
        showToast(message, 'success')
        setAuthRoute('login')
        setAuthPassword('')
        setAuthConfirmPassword('')
        return
      }
      if (authMode === 'forgot') {
        setAuthNotice(payload.message ?? 'If an account exists for that email, a password reset link has been sent.')
        return
      }
      if (authMode === 'reset') {
        setAuthNotice(payload.message ?? 'Your password has been reset. You can now sign in.')
        setAuthRoute('login')
        setAuthPassword('')
        setAuthConfirmPassword('')
        setAuthResetToken('')
        return
      }
      setToken(payload.access_token)
      window.localStorage.setItem(TOKEN_KEY, payload.access_token)
      showToast('Signed in successfully', 'success')
      setAuthPassword('')
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setAuthError(message)
      showToast(message, 'error')
    } finally {
      setAuthBusy(false)
    }
  }

  const resendVerification = async () => {
    setAuthBusy(true)
    setAuthError('')
    try {
      const response = await fetch(`${API_BASE}/auth/resend-verification`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: authEmail }) })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? 'Unable to resend verification email')
      setAuthNotice(payload.message)
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err))
    } finally {
      setAuthBusy(false)
    }
  }

  const logout = () => {
    setToken('')
    setAuthMode('login')
    window.localStorage.removeItem(TOKEN_KEY)
    setProjects([])
    setScans([])
    setSelectedScan(null)
    setView({ kind: 'home' })
    window.history.replaceState({}, '', '/login')
    showToast('Signed out successfully', 'success')
  }

  useEffect(() => {
    if (!token) return
    void loadData().catch((err) => setError(String(err)))
  }, [token])

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
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
  }, [view])

  useEffect(() => {
    if (!loading) return undefined
    const timer = window.setInterval(() => {
      setLiveTick((value) => value + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [loading])

  useEffect(() => {
    const consoleElement = liveLogRef.current
    if (!consoleElement || liveLogs.length === 0) return
    consoleElement.scrollTo({ top: consoleElement.scrollHeight, behavior: 'smooth' })
  }, [liveLogs])

  const runScan = async () => {
    setLoading(true)
    setError('')
    setReport('')
    setLiveLogs([])
    setLivePhase('Starting')
    setLiveOutcome('running')
    setLiveStartedAt(Date.now())
    try {
      const response = await apiFetch('/scan/live', {
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
            setLiveOutcome('completed')
          } else if (payload.type === 'stopped') {
            setReport(payload.report ?? '')
            setLivePhase(payload.message)
            setLiveOutcome('stopped')
          } else if (payload.type === 'error') {
            setLivePhase('Error')
            setLiveOutcome('error')
            throw new Error(payload.message)
          }
        }
      }
      await loadData()
    } catch (err) {
      setLiveOutcome('error')
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const stopScan = async () => {
    try {
      const response = await apiFetch('/scan/live/stop', {
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
    const remainingSeconds = seconds % 60
    if (seconds < 3600) return `${minutes}m ${String(remainingSeconds).padStart(2, '0')}s`
    const hours = Math.floor(seconds / 3600)
    const remainingMinutes = Math.floor((seconds % 3600) / 60)
    return `${hours}h ${String(remainingMinutes).padStart(2, '0')}m ${String(remainingSeconds).padStart(2, '0')}s`
  }
  const liveBars = [
    38, 62, 48, 84, 56, 72, 44, 90, 58, 76, 52, 68,
  ].map((height, index) => ({
    height: `${Math.min(100, height + ((liveTick + index) % 5) * 4)}%`,
    delay: `${index * 90}ms`,
  }))

  useEffect(() => setProjectPage(1), [projectQuery])
  useEffect(() => setScanPage(1), [scanQuery, scanStatusFilter])
  useEffect(() => setProjectHistoryPage(1), [view.kind === 'project' ? view.id : null])

  const completedResult = report ? parseCompletedReport(report) : null
  const completedTarget = completedResult?.targetUrl.replace(/\/$/, '') ?? ''
  const completedScan = report ? [...scans].reverse().find((scan) => scan.url.replace(/\/$/, '') === completedTarget) ?? scans[scans.length - 1] ?? null : null
  const PAGE_SIZE = 5
  const filteredProjects = projects.filter((project) => {
    const query = projectQuery.trim().toLowerCase()
    return !query || `${project.name} ${project.base_url}`.toLowerCase().includes(query)
  })
  const filteredScans = scans.filter((scan) => {
    const query = scanQuery.trim().toLowerCase()
    const matchesQuery = !query || `${scan.url} ${scan.mode} ${scan.status} ${scan.risk_level}`.toLowerCase().includes(query)
    return matchesQuery && (scanStatusFilter === 'all' || scan.status === scanStatusFilter)
  })
  const projectPageCount = Math.max(1, Math.ceil(filteredProjects.length / PAGE_SIZE))
  const scanPageCount = Math.max(1, Math.ceil(filteredScans.length / PAGE_SIZE))
  const visibleProjects = filteredProjects.slice((projectPage - 1) * PAGE_SIZE, projectPage * PAGE_SIZE)
  const visibleScans = filteredScans.slice((scanPage - 1) * PAGE_SIZE, scanPage * PAGE_SIZE)

  const downloadReport = (rawReport: string | null | undefined, filename: string) => {
    if (!rawReport) {
      setError('This scan has no report to export')
      return
    }
    const blob = new Blob([rawReport], { type: 'text/plain;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(link.href)
  }

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
      const response = await apiFetch(`/scans/${id}`)
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
  const projectHistoryPageCount = Math.max(1, Math.ceil(projectScans.length / PAGE_SIZE))
  const visibleProjectScans = [...projectScans]
    .reverse()
    .slice((projectHistoryPage - 1) * PAGE_SIZE, projectHistoryPage * PAGE_SIZE)
  const projectLatestScan = projectScans[projectScans.length - 1] ?? null
  const projectAverageScore = projectScans.length ? Math.round(projectScans.reduce((total, scan) => total + scan.site_score, 0) / projectScans.length) : 0
  const projectTotalFindings = projectScans.reduce((total, scan) => total + scan.total_findings, 0)
  const projectDisplayName = selectedProject ? selectedProject.name.replace(/^https?:\/\//, '').replace(/\/$/, '') : ''
  const latestScan = scans[scans.length - 1] ?? null
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
  const selectedScanReportPages = selectedScan?.raw_report ? parseCompletedReport(selectedScan.raw_report).testedPages : []
  const selectedScanTestedPages = selectedScan
    ? Array.from(
        new Set(
          [
            ...selectedScanReportPages,
            ...selectedScan.findings.map((finding) => finding.url).filter((pageUrl): pageUrl is string => Boolean(pageUrl)),
            selectedScan.url,
          ].map((pageUrl) => pageUrl.trim()).filter(Boolean),
        ),
      )
    : []

  useEffect(() => {
    if (view.kind !== 'scan') return
    setSelectedScan(null)
    if (selectedScan?.id === view.id) return
    void openScanDetail(view.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  const shellClassName = view.kind === 'home' ? 'shell shell-home' : 'shell shell-detail'

  if (!token) {
    const authTitle = authMode === 'login' ? 'Welcome back' : authMode === 'register' ? 'Create your account' : authMode === 'forgot' ? 'Reset your password' : 'Choose a new password'
    const authCopy = authMode === 'login' ? 'Sign in to access your projects, scans, and live testing console.' : authMode === 'register' ? 'Create an account to start running protected website scans.' : authMode === 'forgot' ? 'Enter your email and we will send you a secure password reset link.' : 'Your reset link is ready. Choose a new password for your account.'
    return (
      <main className="auth-shell">
        {toast ? <div className="toast-container app-toast-container top-0 start-50 translate-middle-x p-3"><div className={`toast show app-toast app-toast-${toast.kind}`} role="status" aria-live="polite" aria-atomic="true"><div className="toast-header"><span className="app-toast-dot" aria-hidden="true" /><strong className="me-auto">{toast.kind === 'error' ? 'Action needed' : toast.kind === 'success' ? 'Success' : 'Notice'}</strong><button type="button" className="btn-close btn-close-white" aria-label="Close" onClick={() => setToast(null)} /></div><div className="toast-body">{toast.message}</div><span className="app-toast-timer" aria-hidden="true" /></div></div> : null}
        <section className="auth-card">
          <div className="brand-lockup"><span className="brand-mark" aria-hidden="true"><i /></span><div><p className="eyebrow">Autonomous QA</p><h1>Control Room</h1></div></div>
          <p className="auth-kicker">Secure quality intelligence</p>
          <h2>{authTitle}</h2>
          <p className="auth-copy">{authCopy}</p>
          <form className="auth-form" onSubmit={submitAuth}>
            <label>Email<input type="email" autoComplete="email" value={authEmail} onChange={(event) => setAuthEmail(event.target.value)} required disabled={authMode === 'reset'} /></label>
            {authMode !== 'forgot' ? <label>Password<input type="password" autoComplete={authMode === 'login' ? 'current-password' : 'new-password'} minLength={8} value={authPassword} onChange={(event) => setAuthPassword(event.target.value)} required /></label> : null}
            {authMode === 'register' || authMode === 'reset' ? <label>Confirm password<input type="password" autoComplete="new-password" minLength={8} value={authConfirmPassword} onChange={(event) => setAuthConfirmPassword(event.target.value)} required /></label> : null}
            {authNotice ? <p className="auth-notice">{authNotice}</p> : null}
            {authError ? <p className="auth-error">{authError}</p> : null}
            {authMode === 'login' && authEmail && authError.toLowerCase().includes('verify') ? <button type="button" className="auth-resend" onClick={() => void resendVerification()} disabled={authBusy}>Resend verification email</button> : null}
            {authMode === 'login' ? <button type="button" className="auth-resend" onClick={() => { setAuthRoute('forgot'); setAuthError(''); setAuthNotice('') }}>Forgot password?</button> : null}
            <button className="primary auth-submit" type="submit" disabled={authBusy}>{authBusy ? 'Please wait…' : authMode === 'login' ? 'Sign in' : authMode === 'register' ? 'Create account' : authMode === 'forgot' ? 'Send reset link' : 'Reset password'}</button>
          </form>
          {(authMode === 'login' || authMode === 'register') ? <><div className="auth-divider"><span>or</span></div><button className="google-button" type="button" onClick={() => void signInWithGoogle()} disabled={googleBusy}><span className="google-g" aria-hidden="true">G</span>{googleBusy ? 'Opening Google…' : 'Continue with Google'}</button></> : null}
          <button className="auth-switch" type="button" onClick={() => { setAuthRoute(authMode === 'login' ? 'register' : 'login'); setAuthError(''); setAuthNotice(''); setAuthPassword(''); setAuthConfirmPassword('') }}>{authMode === 'login' ? 'Need an account? Register' : 'Already have an account? Sign in'}</button>
        </section>
      </main>
    )
  }
  return (
    <div className={shellClassName}>
      {toast ? <div className="toast-container app-toast-container top-0 start-50 translate-middle-x p-3"><div className={`toast show app-toast app-toast-${toast.kind}`} role="status" aria-live="polite" aria-atomic="true"><div className="toast-header"><span className="app-toast-dot" aria-hidden="true" /><strong className="me-auto">{toast.kind === 'error' ? 'Action needed' : toast.kind === 'success' ? 'Success' : 'Notice'}</strong><button type="button" className="btn-close btn-close-white" aria-label="Close" onClick={() => setToast(null)} /></div><div className="toast-body">{toast.message}</div><span className="app-toast-timer" aria-hidden="true" /></div></div> : null}
      {view.kind === 'scan' ? (
        <div className="detail-toolbar">
          <button type="button" className="scan-page-back" onClick={closeScanDetail}>← All scans</button>
          <button type="button" className="global-logout" onClick={logout}>Sign out</button>
        </div>
      ) : view.kind === 'project' ? (
        <div className="detail-toolbar">
          <button type="button" className="project-back-link" onClick={closeProjectDetail}>← All projects</button>
          <button type="button" className="global-logout" onClick={logout}>Sign out</button>
        </div>
      ) : null}
      {showDashboard ? (
        <div className="home-layout">
          <aside className="home-sidebar">
            <div className="sidebar-brand">
              <div className="brand-lockup">
                <span className="brand-mark" aria-hidden="true"><i /></span>
                <div>
                  <p className="eyebrow">Autonomous QA</p>
                  <h2>Control Room</h2>
                </div>
              </div>
              <p>Your quality command center.</p>
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
              <button type="button" className="sidebar-link sidebar-link-active"><span aria-hidden="true">◈</span>Overview</button>
              <button type="button" className="sidebar-link" onClick={() => openProject(projects[projects.length - 1]?.id ?? 0)} disabled={!projects.length}>
                <span aria-hidden="true">◇</span>Projects
              </button>
              <button type="button" className="sidebar-link" onClick={() => openScan(scans[scans.length - 1]?.id ?? 0)} disabled={!scans.length}>
                <span aria-hidden="true">↗</span>Scans
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
          <header className="dashboard-header">
            <div>
              <p className="eyebrow">Quality overview</p>
              <h1>Everything looks better when quality is visible.</h1>
              <p>Monitor site health, launch intelligent scans, and turn every finding into a clear next step.</p>
            </div>
            <button type="button" className="global-logout dashboard-logout" onClick={logout}>Sign out</button>
            <div className="dashboard-header-actions">
              <div className={`system-chip ${loading ? 'system-chip-live' : ''}`}>
                <span className="system-chip-dot" />
                <div>
                  <small>System status</small>
                  <strong>{loading ? 'Scan running' : 'All systems operational'}</strong>
                </div>
              </div>
            </div>
          </header>
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
            <article className="intro-panel intro-panel-summary intro-panel-hero">
              <div className="hero-orbit" aria-hidden="true"><span /><i /></div>
              <div className="intro-copy">
                <span>Project snapshot</span>
                <strong>Ship experiences people can trust.</strong>
                <p>Scan every journey, surface meaningful risks, and keep your release quality moving in the right direction.</p>
              </div>
            </article>
            <article className="intro-panel intro-panel-primary signal-panel">
              <span>Quality pulse</span>
              <div className="signal-row"><i className="signal-good" /><div><strong>{passingScans} passing</strong><small>Healthy experiences</small></div></div>
              <div className="signal-row"><i className="signal-warn" /><div><strong>{warningScans} warnings</strong><small>Worth reviewing</small></div></div>
              <div className="signal-row"><i className="signal-alert" /><div><strong>{needsAttentionScans} at risk</strong><small>Needs attention</small></div></div>
            </article>
          </section>

          <section className="panel scan-runner">
            <div className="panel-header">
              <div>
                <p className="eyebrow">New assessment</p>
                <h2>Run a website scan</h2>
                <small>Choose a target and let the quality engine do the rest.</small>
              </div>
              <span className="runner-badge">AI-assisted</span>
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
            {error ? <p className="error" role="alert">{error}</p> : null}
            {(loading || liveLogs.length > 0 || liveOutcome !== 'idle') ? (
              <section className={`live-console live-console-${liveOutcome}`}>
                <header className="live-console-header">
                  <div className="live-console-title">
                    <span className="console-window-dots" aria-hidden="true"><i /><i /><i /></span>
                    <div>
                      <p className="eyebrow">Realtime scanner</p>
                      <h3>Live activity console</h3>
                    </div>
                  </div>
                  <span className="live-console-state" aria-live="polite"><i />{liveOutcome === 'running' ? 'Scanning' : liveOutcome === 'stopped' ? 'Stopped' : liveOutcome === 'error' ? 'Interrupted' : 'Complete'}</span>
                </header>

                <div className="live-console-overview">
                  <div className="live-console-phase">
                    <span>Current operation</span>
                    <strong>{livePhase}</strong>
                  </div>
                  <div className="live-console-stat">
                    <span>Elapsed</span>
                    <strong>{formatElapsed(liveElapsedSeconds)}</strong>
                  </div>
                  <div className="live-console-stat">
                    <span>Events</span>
                    <strong>{liveLogs.length}</strong>
                  </div>
                  <div className="live-console-waveform" aria-label={loading ? 'Scan activity is live' : 'Scan activity stopped'}>
                    {liveBars.map((bar, index) => <i key={index} className={loading ? 'is-active' : ''} style={{ height: bar.height, animationDelay: bar.delay }} />)}
                  </div>
                </div>

                <div className="live-console-terminal">
                  <div className="live-console-toolbar">
                    <span><i className="console-online-dot" />Event stream</span>
                    <span>Auto-scroll enabled</span>
                  </div>
                  <ul className="live-console-lines" ref={liveLogRef}>
                    {liveLogs.length ? liveLogs.map((line, index) => {
                      const event = classifyLiveLog(line)
                      return (
                        <li key={`${index}-${line}`} className={`live-console-line log-${event.kind}`}>
                          <span className="console-line-number">{String(index + 1).padStart(2, '0')}</span>
                          <span className="console-event-icon" aria-hidden="true">{event.icon}</span>
                          <span className="console-line-message">{line}</span>
                          <span className="console-event-label">{event.label}</span>
                        </li>
                      )
                    }) : (
                      <li className="live-console-empty"><span className="console-loader" /><span>Preparing the scanner and waiting for the first event…</span></li>
                    )}
                  </ul>
                </div>

                <footer className="live-console-footer">
                  <span><i />{url || 'Waiting for a target URL'}</span>
                  <span>{mode} · {headless ? 'headless' : 'visible browser'}</span>
                </footer>
              </section>
            ) : null}
            {completedResult ? (
              <section className={`completed-report ${liveOutcome === 'stopped' ? 'completed-report-stopped' : ''}`}>
                <header className="completed-report-header">
                  <div className="completed-report-heading">
                    <span className="completed-report-mark" aria-hidden="true">{liveOutcome === 'stopped' ? '■' : '✓'}</span>
                    <div><p className="eyebrow">{liveOutcome === 'stopped' ? 'Assessment stopped' : 'Assessment complete'}</p><h2>{liveOutcome === 'stopped' ? 'Your partial quality report is saved' : 'Your quality report is ready'}</h2><p>{completedResult.targetUrl || url}</p></div>
                  </div>
                  <div className="completed-report-actions">
                    <span className="completed-report-status"><i />{liveOutcome === 'stopped' ? 'Partial results saved' : 'Saved successfully'}</span>
                    {completedScan ? <button onClick={() => openScan(completedScan.id)}>Open scan details <span aria-hidden="true">→</span></button> : null}
                  </div>
                </header>

                <div className="completed-report-spotlight">
                  <div className="completed-score-ring" style={{ background: `conic-gradient(#8b7cff ${completedResult.siteScore}%, rgba(255,255,255,.07) 0)` }}>
                    <div><strong>{completedResult.siteScore}</strong><span>out of 100</span></div>
                  </div>
                  <div className="completed-score-copy">
                    <span className={`completed-risk completed-risk-${completedResult.riskLevel.toLowerCase().replace(/[^a-z]+/g, '-')}`}>{completedResult.riskLevel} risk</span>
                    <h3>{liveOutcome === 'stopped' ? 'Partial quality signal' : completedResult.siteScore >= 90 ? 'Excellent quality signal' : completedResult.siteScore >= 70 ? 'A solid result with room to improve' : 'This experience needs attention'}</h3>
                    <p>{completedResult.executiveSummary[0] ?? `${completedResult.pagesTested} pages were tested and ${completedResult.totalFindings} findings were recorded.`}</p>
                  </div>
                  <div className="completed-report-mini-metrics">
                    <div><span>Pages</span><strong>{completedResult.pagesTested}</strong></div>
                    <div><span>Unique</span><strong>{completedResult.uniqueFindings}</strong></div>
                    <div><span>Findings</span><strong>{completedResult.totalFindings}</strong></div>
                  </div>
                </div>

                <div className="completed-report-metrics">
                  <article><span>Broken links</span><strong>{completedResult.brokenLinks}</strong><i className={completedResult.brokenLinks ? 'metric-alert' : 'metric-good'} /></article>
                  <article><span>JavaScript errors</span><strong>{completedResult.jsErrors}</strong><i className={completedResult.jsErrors ? 'metric-alert' : 'metric-good'} /></article>
                  <article><span>API failures</span><strong>{completedResult.apiFailures}</strong><i className={completedResult.apiFailures ? 'metric-alert' : 'metric-good'} /></article>
                  <article><span>Resource failures</span><strong>{completedResult.resourceFailures}</strong><i className={completedResult.resourceFailures ? 'metric-warn' : 'metric-good'} /></article>
                  <article><span>Navigation failures</span><strong>{completedResult.navigationFailures}</strong><i className={completedResult.navigationFailures ? 'metric-alert' : 'metric-good'} /></article>
                  <article><span>Slow pages</span><strong>{completedResult.slowPages}</strong><i className={completedResult.slowPages ? 'metric-warn' : 'metric-good'} /></article>
                </div>

                <div className="completed-report-grid">
                  <article className="completed-report-card completed-summary-card">
                    <div className="completed-card-heading"><div><p className="eyebrow">At a glance</p><h3>Executive summary</h3></div><span aria-hidden="true">✦</span></div>
                    {completedResult.executiveSummary.length ? (
                      <ul>{completedResult.executiveSummary.map((item, index) => <li key={index}><i />{item}</li>)}</ul>
                    ) : <p className="completed-empty-copy">No executive summary was generated for this scan.</p>}
                  </article>

                  <article className="completed-report-card completed-pages-card">
                    <div className="completed-card-heading"><div><p className="eyebrow">Coverage</p><h3>Pages tested</h3></div><span>{completedResult.testedPages.length}</span></div>
                    {completedResult.testedPages.length ? (
                      <ul>{completedResult.testedPages.slice(0, 6).map((pageUrl, index) => <li key={`${index}-${pageUrl}`}><span>{String(index + 1).padStart(2, '0')}</span><strong>{pageUrl}</strong></li>)}</ul>
                    ) : <p className="completed-empty-copy">No tested-page list was included.</p>}
                    {completedResult.testedPages.length > 6 ? <small>+{completedResult.testedPages.length - 6} additional pages available in the raw report</small> : null}
                  </article>
                </div>

                {completedResult.scoreBreakdown.length ? (
                  <article className="completed-report-card completed-breakdown-card">
                    <div className="completed-card-heading"><div><p className="eyebrow">Quality dimensions</p><h3>Score breakdown</h3></div><span>{completedResult.scoreBreakdown.length} categories</span></div>
                    <div className="completed-score-groups">
                      {completedResult.scoreBreakdown.map((group) => (
                        <div key={group.label} className="completed-score-group">
                          <div><span>{group.label}</span><strong>{group.score}/100</strong></div>
                          <span className="completed-score-track"><i style={{ width: `${group.score}%` }} /></span>
                          {group.deductions.length ? <small>{group.deductions[0]}</small> : <small>No deductions</small>}
                        </div>
                      ))}
                    </div>
                  </article>
                ) : null}

                {completedResult.findings.length ? (
                  <article className="completed-report-card completed-findings-card">
                    <div className="completed-card-heading"><div><p className="eyebrow">Evidence</p><h3>Key findings</h3></div><span>{completedResult.findings.length}</span></div>
                    <ul>{completedResult.findings.slice(0, 5).map((finding, index) => <li key={index}><span>{finding.severity}</span><strong>{finding.message}</strong></li>)}</ul>
                    {completedResult.findings.length > 5 ? <small>+{completedResult.findings.length - 5} additional findings available in Scan Detail</small> : null}
                  </article>
                ) : null}

                <details className="completed-raw-report">
                  <summary><span><span className="eyebrow">Technical output</span><strong>{liveOutcome === 'stopped' ? 'Partial raw report' : 'Complete raw report'}</strong></span><span>View output <i aria-hidden="true">⌄</i></span></summary>
                  <div className="report-toolbar"><button type="button" className="secondary compact-button" onClick={() => downloadReport(report, `scan-report-${completedScan?.id ?? 'latest'}.txt`)}>↓ Export report</button></div>
                  <pre>{report}</pre>
                </details>
              </section>
            ) : null}
          </section>

          <section className="home-grid">
            <div className="panel fixed-panel">
              <div className="panel-header"><div><h2>Projects</h2><small>{filteredProjects.length} matching</small></div><span>{projects.length}</span></div>
              <div className="list-tools"><input value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder="Search projects" aria-label="Search projects" /></div>
              <ul className="list scroll-list">
                {visibleProjects.map((project) => (
                  <li key={project.id} onClick={() => openProject(project.id)} className="clickable">
                    <div className="list-row-top"><strong>{project.name}</strong><span className="list-badge">Project</span></div><span>{project.base_url}</span>
                  </li>
                ))}
              </ul>
              <div className="list-pagination"><span>Page {projectPage} of {projectPageCount}</span><div><button type="button" disabled={projectPage <= 1} onClick={() => setProjectPage((page) => page - 1)}>←</button><button type="button" disabled={projectPage >= projectPageCount} onClick={() => setProjectPage((page) => page + 1)}>→</button></div></div>
            </div>

            <div className="panel fixed-panel">
              <div className="panel-header"><div><h2>Scans</h2><small>{filteredScans.length} matching</small></div><span>{scans.length}</span></div>
              <div className="list-tools"><input value={scanQuery} onChange={(event) => setScanQuery(event.target.value)} placeholder="Search scans" aria-label="Search scans" /><select value={scanStatusFilter} onChange={(event) => setScanStatusFilter(event.target.value)} aria-label="Filter scans by status"><option value="all">All statuses</option><option value="completed">Completed</option><option value="stopped">Stopped</option><option value="failed">Failed</option><option value="running">Running</option></select></div>
              <ul className="list scroll-list">
                {visibleScans.map((scan) => (
                  <li key={scan.id} onClick={() => openScan(scan.id)} className="clickable"><div className="list-row-top"><strong>{scan.url}</strong><span className={`list-badge list-badge-score ${scan.site_score >= 90 ? 'list-badge-good' : scan.site_score >= 70 ? 'list-badge-warn' : 'list-badge-alert'}`}>{scan.site_score}/100</span></div><span>{scan.status} · {scan.pages_tested} pages · {scan.total_findings} findings · unique {scan.unique_findings}</span></li>
                ))}
              </ul>
              <div className="list-pagination"><span>Page {scanPage} of {scanPageCount}</span><div><button type="button" disabled={scanPage <= 1} onClick={() => setScanPage((page) => page - 1)}>←</button><button type="button" disabled={scanPage >= scanPageCount} onClick={() => setScanPage((page) => page + 1)}>→</button></div></div>
            </div>
          </section>
          </main>
        </div>
      ) : null}

      {view.kind === 'scan' ? (
        <section className="scan-page">
          <header className="scan-page-hero">
            <div className="scan-page-topline">
              <span className={`scan-state ${selectedScan?.status === 'completed' ? 'scan-state-complete' : selectedScan?.status === 'stopped' ? 'scan-state-stopped' : 'scan-state-running'}`}>
                <i /> {selectedScan?.status ?? (scanLoading ? 'Loading' : 'Unavailable')}
              </span>
            </div>
            <div className="scan-page-hero-main">
              <div className="scan-page-identity">
                <span className="scan-page-avatar" aria-hidden="true">S{selectedScan?.id ?? view.id}</span>
                <div>
                  <p className="eyebrow">Scan detail</p>
                  <h1>{selectedScan ? `Scan #${selectedScan.id}` : `Scan #${view.id}`}</h1>
                  <a href={selectedScan?.url ?? '#'} target="_blank" rel="noreferrer">{selectedScan?.url ?? 'Loading scan details…'} <span aria-hidden="true">↗</span></a>
                </div>
              </div>
              {selectedScan?.project_id ? (
                <button className="secondary scan-project-action" onClick={() => openProject(selectedScan.project_id as number)}>
                  View project <span aria-hidden="true">→</span>
                </button>
              ) : null}
            </div>
          </header>

          <section className="scan-page-metrics" aria-label="Scan metrics">
            <article>
              <span>Quality score</span>
              <strong>{selectedScan ? `${selectedScan.site_score}/100` : '—'}</strong>
              <small>{selectedScan?.risk_level ?? 'Loading quality signal'}</small>
            </article>
            <article>
              <span>Pages tested</span>
              <strong>{selectedScan?.pages_tested ?? '—'}</strong>
              <small>Pages included in this run</small>
            </article>
            <article>
              <span>Total findings</span>
              <strong>{selectedScan?.total_findings ?? '—'}</strong>
              <small>{selectedScan ? `${selectedScan.unique_findings} unique after dedupe` : 'Loading findings'}</small>
            </article>
            <article>
              <span>Risk level</span>
              <strong>{selectedScan?.risk_level ?? '—'}</strong>
              <small>{selectedScan?.comparison?.comparison_note ?? 'Current assessment'}</small>
            </article>
          </section>

          <section className="scan-page-workspace">
            <aside className="scan-page-side">
              <article className="scan-page-card scan-run-profile">
                <div className="scan-page-card-heading">
                  <div><p className="eyebrow">Run profile</p><h2>Execution details</h2></div>
                  <span className="scan-page-card-icon" aria-hidden="true">⌁</span>
                </div>
                {selectedScan ? (
                  <dl className="scan-run-facts">
                    <div><dt>Status</dt><dd><span className="scan-inline-status"><i />{selectedScan.status}</span></dd></div>
                    <div><dt>Mode</dt><dd>{selectedScan.mode}</dd></div>
                    <div><dt>Browser visibility</dt><dd>{selectedScan.headless ? 'Headless' : 'Visible browser'}</dd></div>
                    <div><dt>Started</dt><dd>{formatProjectDate(selectedScan.started_at)}</dd></div>
                    <div><dt>Finished</dt><dd>{selectedScan.finished_at ? formatProjectDate(selectedScan.finished_at) : 'Still running'}</dd></div>
                    <div><dt>Scan ID</dt><dd>#{selectedScan.id}</dd></div>
                  </dl>
                ) : <p className="scan-empty">Loading execution details…</p>}
              </article>

              <article className="scan-page-card scan-issue-card">
                <div className="scan-page-card-heading">
                  <div><p className="eyebrow">Issue profile</p><h2>Failure signals</h2></div>
                </div>
                {selectedScan ? (
                  <div className="scan-issue-list">
                    <div><span><i className="issue-red" />Navigation</span><strong>{selectedScan.navigation_failures}</strong></div>
                    <div><span><i className="issue-orange" />Slow pages</span><strong>{selectedScan.slow_pages}</strong></div>
                    <div><span><i className="issue-blue" />JavaScript</span><strong>{selectedScan.js_errors}</strong></div>
                    <div><span><i className="issue-purple" />API failures</span><strong>{selectedScan.api_failures}</strong></div>
                    <div><span><i className="issue-green" />Broken links</span><strong>{selectedScan.broken_links}</strong></div>
                    <div><span><i className="issue-gray" />Resources</span><strong>{selectedScan.resource_failures}</strong></div>
                  </div>
                ) : null}
              </article>
            </aside>

            <div className="scan-page-main">
              <article className="scan-page-card scan-quality-card">
                <div className="scan-page-card-heading">
                  <div><p className="eyebrow">Quality intelligence</p><h2>Assessment summary</h2></div>
                  <span className="scan-quality-label">Latest result</span>
                </div>
                {selectedScan ? (
                  <div className="scan-quality-content">
                    <div className="scan-quality-ring" style={{ background: `conic-gradient(#38bdf8 ${selectedScan.site_score}%, rgba(255,255,255,.07) 0)` }}>
                      <div><strong>{selectedScan.site_score}</strong><span>out of 100</span></div>
                    </div>
                    <div className="scan-quality-copy">
                      <span className={`list-badge list-badge-score ${selectedScan.site_score >= 90 ? 'list-badge-good' : selectedScan.site_score >= 70 ? 'list-badge-warn' : 'list-badge-alert'}`}>{selectedScan.risk_level}</span>
                      <h3>{selectedScan.executive_summary ?? selectedScan.phase2_summary ?? `${selectedScan.pages_tested} pages were assessed and ${selectedScan.total_findings} findings were recorded.`}</h3>
                      <div className="scan-quality-stats">
                        <div><span>Unique</span><strong>{selectedScan.unique_findings}</strong></div>
                        <div><span>Missing UI</span><strong>{selectedScan.missing_elements}</strong></div>
                        <div><span>Third party</span><strong>{selectedScan.third_party_failures}</strong></div>
                        <div><span>Page URLs</span><strong>{selectedScanTestedPages.length}</strong></div>
                      </div>
                    </div>
                  </div>
                ) : <div className="scan-page-loading">Loading assessment intelligence…</div>}
              </article>

              <article className="scan-page-card scan-findings-card">
                <div className="scan-page-card-heading">
                  <div><p className="eyebrow">Evidence</p><h2>Findings by category</h2></div>
                  <span className="scan-quality-label">{selectedScan?.findings.length ?? 0} findings</span>
                </div>
                {selectedScan && groupedScanFindings.length ? (
                  <div className="scan-finding-groups">
                    {groupedScanFindings.map(([category, findings], groupIndex) => (
                      <details key={category} className="scan-finding-group" open={groupIndex === 0}>
                        <summary>
                          <span className="scan-finding-category"><i />{category.replace(/_/g, ' ')}</span>
                          <span><strong>{findings.length}</strong><i aria-hidden="true">⌄</i></span>
                        </summary>
                        <ul>
                          {findings.slice(0, 5).map((finding) => (
                            <li key={finding.id}>
                              <span className="scan-finding-number">{String(finding.id).padStart(2, '0')}</span>
                              <div><strong>{finding.message}</strong><small>{finding.url ?? 'Global finding'}{finding.note ? ` · ${finding.note}` : ''}</small></div>
                            </li>
                          ))}
                          {findings.length > 5 ? <li className="scan-finding-more">+{findings.length - 5} additional findings in this category</li> : null}
                        </ul>
                      </details>
                    ))}
                  </div>
                ) : (
                  <div className="scan-page-empty"><span aria-hidden="true">✓</span><div><h3>No findings captured</h3><p>This scan did not record any actionable issues.</p></div></div>
                )}
              </article>

              <article className="scan-page-card scan-score-card">
                <div className="scan-page-card-heading">
                  <div><p className="eyebrow">Scoring</p><h2>Score breakdown</h2></div>
                  <span className="scan-quality-label">{selectedScan?.site_score ?? 0}/100</span>
                </div>
                {selectedScan?.score_breakdown.length ? (
                  <div className="scan-score-grid">
                    {selectedScan.score_breakdown.map((group) => (
                      <details key={group.label} className="scan-score-group">
                        <summary><span>{group.label}</span><strong>{group.score}/100</strong></summary>
                        <ul>{group.deductions.length ? group.deductions.map((deduction, index) => <li key={`${group.label}-${index}`}>{deduction}</li>) : <li>No deductions</li>}</ul>
                      </details>
                    ))}
                  </div>
                ) : <div className="scan-page-empty compact-scan-empty"><span aria-hidden="true">◎</span><div><h3>No score breakdown</h3><p>This scan does not include category-level scoring data.</p></div></div>}
              </article>

              <div className="scan-support-grid">
                <article className="scan-page-card">
                  <div className="scan-page-card-heading"><div><p className="eyebrow">Progress</p><h2>Previous comparison</h2></div></div>
                  {selectedScan?.comparison ? (
                    <div className="scan-comparison-grid">
                      <div><span>Previous scan</span><strong>{selectedScan.comparison.previous_scan_id ? `#${selectedScan.comparison.previous_scan_id}` : '—'}</strong></div>
                      <div><span>Previous score</span><strong>{selectedScan.comparison.previous_score ?? '—'}</strong></div>
                      <div><span>Score change</span><strong>{selectedScan.comparison.score_delta ?? '—'}</strong></div>
                      <div><span>Previous risk</span><strong>{selectedScan.comparison.previous_risk_level ?? '—'}</strong></div>
                    </div>
                  ) : <p className="scan-support-empty">No previous scan is available for comparison.</p>}
                </article>
                <article className="scan-page-card">
                  <div className="scan-page-card-heading"><div><p className="eyebrow">Coverage</p><h2>Tested pages</h2></div><span className="scan-quality-label">{selectedScanTestedPages.length}</span></div>
                  {selectedScanTestedPages.length ? (
                    <ul className="scan-tested-page-list">{selectedScanTestedPages.map((pageUrl, index) => <li key={`${index}-${pageUrl}`}><span>{String(index + 1).padStart(2, '0')}</span><a href={pageUrl} target="_blank" rel="noreferrer">{pageUrl}</a></li>)}</ul>
                  ) : <p className="scan-support-empty">No tested page URLs were stored for this scan.</p>}
                </article>
              </div>

              <details className="scan-page-card scan-raw-card">
                <summary><span><span className="eyebrow">Technical output</span><strong>Raw scan report</strong></span><span>View full output <i aria-hidden="true">⌄</i></span></summary>
                <div className="report-toolbar"><button type="button" className="secondary compact-button" onClick={() => downloadReport(selectedScan?.raw_report, `scan-report-${selectedScan?.id ?? view.id}.txt`)}>↓ Export report</button></div>
                {selectedScan?.raw_report ? <pre>{selectedScan.raw_report}</pre> : <p className="scan-support-empty">No raw report was stored for this scan.</p>}
              </details>
            </div>
          </section>
        </section>
      ) : null}

      {view.kind === 'project' && selectedProject ? (
        <section className="project-page">
          <header className="project-hero">
            <div className="project-hero-topline">
              <span className={`project-health ${projectLatestScan && projectLatestScan.site_score >= 90 ? 'project-health-good' : projectLatestScan && projectLatestScan.site_score >= 70 ? 'project-health-warn' : 'project-health-alert'}`}>
                <i /> {projectLatestScan ? projectLatestScan.risk_level : 'Awaiting first scan'}
              </span>
            </div>
            <div className="project-hero-main">
              <div className="project-identity">
                <span className="project-avatar" aria-hidden="true">{projectDisplayName.slice(0, 2).toUpperCase()}</span>
                <div>
                  <p className="eyebrow">Project detail</p>
                  <h1>{projectDisplayName}</h1>
                  <a href={selectedProject.base_url} target="_blank" rel="noreferrer">{selectedProject.base_url} <span aria-hidden="true">↗</span></a>
                </div>
              </div>
              <button
                className="project-scan-action"
                onClick={() => {
                  setUrl(selectedProject.base_url)
                  closeProjectDetail()
                }}
              >
                <span aria-hidden="true">＋</span> Run new scan
              </button>
            </div>
          </header>

          <section className="project-metrics" aria-label="Project metrics">
            <article>
              <span>Latest score</span>
              <strong>{projectLatestScan ? `${projectLatestScan.site_score}/100` : '—'}</strong>
              <small>{projectLatestScan ? projectLatestScan.risk_level : 'No scan data yet'}</small>
            </article>
            <article>
              <span>Total scans</span>
              <strong>{projectScans.length}</strong>
              <small>{projectScans.length === 1 ? 'Assessment completed' : 'Assessments completed'}</small>
            </article>
            <article>
              <span>Average score</span>
              <strong>{projectScans.length ? `${projectAverageScore}/100` : '—'}</strong>
              <small>Across all project scans</small>
            </article>
            <article>
              <span>Total findings</span>
              <strong>{projectTotalFindings}</strong>
              <small>{projectLatestScan ? `${projectLatestScan.unique_findings} unique in latest scan` : 'Nothing recorded yet'}</small>
            </article>
          </section>

          <section className="project-workspace">
            <aside className="project-side-column">
              <article className="project-card project-info-card">
                <div className="project-card-heading">
                  <div>
                    <p className="eyebrow">Project profile</p>
                    <h2>About this project</h2>
                  </div>
                  <span className="project-card-icon" aria-hidden="true">◇</span>
                </div>
                <p className="project-card-copy">Scans for this base URL are grouped here so you can track quality over time.</p>
                <dl className="project-facts">
                  <div><dt>Base URL</dt><dd>{selectedProject.base_url}</dd></div>
                  <div><dt>Created</dt><dd>{formatProjectDate(selectedProject.created_at)}</dd></div>
                  <div><dt>Last updated</dt><dd>{formatProjectDate(selectedProject.updated_at)}</dd></div>
                  <div><dt>Project ID</dt><dd>#{selectedProject.id}</dd></div>
                </dl>
              </article>

              <article className="project-card project-next-card">
                <span className="project-next-icon" aria-hidden="true">↗</span>
                <p className="eyebrow">Recommended action</p>
                <h2>{projectLatestScan ? 'Keep your quality signal fresh' : 'Establish your quality baseline'}</h2>
                <p>{projectLatestScan ? 'Run another scan after your next release to compare results and catch regressions.' : 'Run the first scan to generate a score, findings, and project health summary.'}</p>
                <button
                  className="secondary"
                  onClick={() => {
                    setUrl(selectedProject.base_url)
                    closeProjectDetail()
                  }}
                >
                  Configure scan <span aria-hidden="true">→</span>
                </button>
              </article>
            </aside>

            <div className="project-main-column">
              <article className="project-card project-latest-card">
                <div className="project-card-heading">
                  <div>
                    <p className="eyebrow">Latest assessment</p>
                    <h2>Current quality signal</h2>
                  </div>
                  {projectLatestScan ? <span className="project-scan-id">Scan #{projectLatestScan.id}</span> : null}
                </div>
                {projectLatestScan ? (
                  <div className="project-latest-content">
                    <div
                      className="project-score-ring"
                      style={{ background: `conic-gradient(#8b7cff ${projectLatestScan.site_score}%, rgba(255,255,255,.07) 0)` }}
                    >
                      <div><strong>{projectLatestScan.site_score}</strong><span>out of 100</span></div>
                    </div>
                    <div className="project-latest-summary">
                      <span className={`list-badge list-badge-score ${projectLatestScan.site_score >= 90 ? 'list-badge-good' : projectLatestScan.site_score >= 70 ? 'list-badge-warn' : 'list-badge-alert'}`}>
                        {projectLatestScan.risk_level}
                      </span>
                      <h3>{projectLatestScan.phase2_summary ?? `${projectLatestScan.pages_tested} pages assessed with ${projectLatestScan.unique_findings} unique findings.`}</h3>
                      <div className="project-latest-stats">
                        <div><span>Pages</span><strong>{projectLatestScan.pages_tested}</strong></div>
                        <div><span>Findings</span><strong>{projectLatestScan.total_findings}</strong></div>
                        <div><span>Unique</span><strong>{projectLatestScan.unique_findings}</strong></div>
                        <div><span>Mode</span><strong>{projectLatestScan.mode}</strong></div>
                      </div>
                    </div>
                    <button className="project-open-scan" onClick={() => openScan(projectLatestScan.id)} aria-label={`Open scan ${projectLatestScan.id}`}>→</button>
                  </div>
                ) : (
                  <div className="project-empty-state">
                    <span aria-hidden="true">◎</span>
                    <h3>No scans yet</h3>
                    <p>Run your first assessment to populate this quality overview.</p>
                  </div>
                )}
              </article>

              <article className="project-card project-history-card">
                <div className="project-card-heading">
                  <div>
                    <p className="eyebrow">Scan history</p>
                    <h2>Recent assessments</h2>
                  </div>
                  <span className="project-history-count">{projectScans.length} total</span>
                </div>
                {projectScans.length ? (
                  <div className="project-scan-table">
                    <div className="project-scan-table-head"><span>Assessment</span><span>Health</span><span>Coverage</span><span>Started</span><span /></div>
                    {visibleProjectScans.map((scan) => (
                      <button key={scan.id} className="project-scan-row" onClick={() => openScan(scan.id)}>
                        <span><strong>Scan #{scan.id}</strong><small>{scan.status}</small></span>
                        <span><strong>{scan.site_score}/100</strong><small>{scan.risk_level}</small></span>
                        <span><strong>{scan.pages_tested} pages</strong><small>{scan.unique_findings} unique findings</small></span>
                        <span><strong>{formatProjectDate(scan.started_at)}</strong><small>{scan.mode}</small></span>
                        <i aria-hidden="true">→</i>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="project-empty-state compact-empty"><p>No scan history is available for this project.</p></div>
                )}
                {projectScans.length ? (
                  <div className="project-history-pagination">
                    <span>Page {projectHistoryPage} of {projectHistoryPageCount}</span>
                    <div>
                      <button type="button" disabled={projectHistoryPage <= 1} onClick={() => setProjectHistoryPage((page) => page - 1)} aria-label="Previous scan history page">←</button>
                      <button type="button" disabled={projectHistoryPage >= projectHistoryPageCount} onClick={() => setProjectHistoryPage((page) => page + 1)} aria-label="Next scan history page">→</button>
                    </div>
                  </div>
                ) : null}
              </article>
            </div>
          </section>
        </section>
      ) : null}
    </div>
  )
}
