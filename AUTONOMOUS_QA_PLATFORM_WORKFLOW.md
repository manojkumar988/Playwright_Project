# Autonomous QA Platform Workflow

This document explains the repository workflow, runtime behavior, and important paths for the Autonomous QA Platform.

## Repository Map

| Path | Purpose |
| --- | --- |
| `README.md` | Setup, commands, endpoint overview, and deployment notes. |
| `pyproject.toml` | Python package metadata and dependencies for the scanner/backend. |
| `.env.example` | Backend environment variable template. |
| `qa_schema.sql` | Older/reference SQL schema. The live app uses SQLAlchemy models in `backend/orm_models.py`. |
| `backend/` | FastAPI API, authentication, persistence, email, and scan orchestration. |
| `backend/app.py` | Main FastAPI application and all HTTP endpoints. |
| `backend/main.py` | Uvicorn launcher for `backend.app:app`. |
| `backend/db.py` | SQLAlchemy engine/session setup and startup schema initialization. |
| `backend/orm_models.py` | Database tables for pending signups, users, projects, scans, findings, and artifacts. |
| `backend/security.py` | Password hashing, JWT creation/validation, current-user dependency, and public URL validation. |
| `backend/email_service.py` | Email verification and password reset token/email helpers. |
| `src/qa_platform/` | Core scanner package and CLI. |
| `src/qa_platform/__main__.py` | CLI entry point for `python -m qa_platform`. |
| `src/qa_platform/scanner.py` | Main website crawler/scanner implementation. |
| `src/qa_platform/reporting.py` | Converts scanner results into the raw text report used by the API and UI. |
| `src/qa_platform/models.py` | Dataclasses for evidence, findings, page summaries, and reports. |
| `frontend/` | React/Vite dashboard. |
| `frontend/src/App.tsx` | Main frontend app, auth screens, scan runner, live console, dashboard, project pages, and scan detail pages. |
| `frontend/src/main.tsx` | React bootstrap, Bootstrap CSS import, and app mount. |
| `frontend/src/styles.css` | Dashboard/auth/detail page styling. |
| `frontend/package.json` | Frontend dependencies and scripts. |
| `deploy/Caddyfile` | HTTPS reverse proxy example for production. |
| `tests/test_phase1_scaffold.py` | Unit tests for scanner/report behavior and stopped scan handling. |

## System Architecture

The platform has three main layers:

1. Scanner package in `src/qa_platform`
2. FastAPI backend in `backend`
3. React dashboard in `frontend`

The scanner can run by itself from the CLI, or the backend can run it for authenticated dashboard users. The backend persists scan results into a relational database. The frontend consumes the backend API, streams live scan logs, and renders projects, scans, score breakdowns, findings, and raw reports.

## Setup Workflow

1. Create and activate a Python virtual environment.
2. Install the Python package:

```bash
pip install -e .
```

3. Install frontend packages:

```bash
cd frontend
npm install
```

4. Create environment files:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

5. Configure at least:

| Variable | Used By | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | Backend | SQLAlchemy database URL. Defaults to local PostgreSQL database `QA`. |
| `JWT_SECRET` | Backend | HMAC secret for access tokens. Must be changed in production. |
| `CORS_ORIGINS` | Backend | Allowed frontend origins. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS` | Backend | Email delivery for verification and password reset. |
| `BACKEND_URL` | Backend email links | Public backend URL used in email verification links. |
| `FRONTEND_URL` | Backend redirects/emails | Public frontend URL used after verification, OAuth, and password reset. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Backend | Optional Google sign-in configuration. |
| `VITE_API_BASE` | Frontend | Backend API origin. Defaults to `http://127.0.0.1:8000`. |

## Running The Project

Backend:

```bash
uvicorn backend.app:app --reload
```

Alternative backend launcher:

```bash
python -m backend.main
```

Frontend:

```bash
cd frontend
npm run dev
```

CLI scanner:

```bash
python -m qa_platform https://example.com --mode browser --headless
```

Frontend production build:

```bash
cd frontend
npm run build
```

Tests:

```bash
python -m unittest tests/test_phase1_scaffold.py
```

## CLI Scanner Flow

CLI entry path: `src/qa_platform/__main__.py`

1. Parses `url`, `--mode`, and `--headless`.
2. Converts `browser-fast` into browser mode with `fast_browser=True`.
3. Instantiates `Phase1Tester` from `src/qa_platform/scanner.py`.
4. Runs the scan.
5. Prints `format_raw_report(report)` from `src/qa_platform/reporting.py`.

Supported modes:

| Mode | Behavior |
| --- | --- |
| `auto` | Uses Playwright browser scanning when available, otherwise HTTP crawling. |
| `browser` | Uses Playwright browser automation if installed. |
| `browser-fast` | Uses Playwright with reduced scrolling/action exploration. |
| `http` | Uses `urllib` HTTP crawling only. |

## Scanner Working

Main class: `Phase1Tester` in `src/qa_platform/scanner.py`

High-level flow:

1. Normalize the target URL.
2. Create a `TestReport`.
3. Choose browser or HTTP mode.
4. Crawl up to `MAX_PAGES = 30`.
5. Record page summaries, tested URLs, clicked URLs, recordings, findings, and evidence.
6. Deduplicate findings.
7. Calculate category scores, weighted site score, risk level, phase summary, and executive summary.

HTTP crawl path:

1. Start from the normalized base URL.
2. Fetch pages with `urllib.request.urlopen`.
3. Parse HTML with `_PageParser`.
4. Collect links from anchors, images, and scripts.
5. Keep crawl within the same host.
6. Skip static file extensions such as CSS, JS, images, fonts, maps, videos, JSON, XML, and text files.
7. Detect broken pages, slow pages, JavaScript error markers, API-like failures, and missing-content conditions.

Browser scan path:

1. Launch Chromium through Playwright.
2. Open a browser context and page.
3. Attach console, page error, and request failure listeners.
4. Visit queued pages.
5. Dismiss common popups/cookie banners.
6. Scroll pages unless `browser-fast` is enabled.
7. Extract regular links, navigation links, DOM/data-href/form links, and visible clickable links.
8. Exercise up to `MAX_ACTIONS_PER_PAGE = 8` visible link actions per page.
9. Recover from closed pages/contexts where possible.
10. Capture screenshots for browser evidence and video paths when Playwright provides recordings.
11. Classify console/network failures as JavaScript, resource, third-party, API, or navigation findings.

Safety behavior:

1. `backend/security.py` rejects scan URLs resolving to localhost, private, link-local, reserved, multicast, unspecified, `.local`, or `.localhost` hosts.
2. `scanner.py` also checks browser/HTTP redirects and blocks redirects to private/internal targets.
3. HTTP crawling stays on the same netloc as the starting URL.

## Report And Scoring Workflow

Report model path: `src/qa_platform/models.py`

Important report fields:

| Field | Meaning |
| --- | --- |
| `target_url` | Original scan target. |
| `pages_tested` | Count of pages visited. |
| `tested_urls` | URLs visited by the scanner. |
| `clicked_urls` | URLs reached through browser action clicks. |
| `page_summaries` | URL, title, status, response time, parent, and duplicate metadata. |
| `recordings` | Browser video artifact paths. |
| `findings` | Raw findings. |
| `unique_findings` | Deduplicated findings. |
| `site_score` | Weighted score from 0 to 100. |
| `risk_level` | Low, Moderate, Moderate-High, High, or Critical. |
| `phase2_summary` | Short machine-readable scan summary. |
| `executive_summary` | Human-readable website health summary and recommendations. |

Scoring weights:

| Category | Weight |
| --- | --- |
| Functional Quality | 40% |
| Performance | 25% |
| Navigation | 15% |
| Resource Health | 10% |
| API Health | 10% |

Finding impact examples:

| Finding Category | Main Score Area |
| --- | --- |
| `broken_link` | Functional Quality |
| `js_error` | Functional Quality |
| `missing_element` | Functional Quality |
| `slow_page` | Performance |
| `navigation_failure` | Navigation |
| `resource_failure` | Resource Health |
| `third_party_failure` | Resource Health |
| `api_failure` | API Health |

`src/qa_platform/reporting.py` formats the final text report with target URL, tested pages, clicked links, page summaries, crawl graph, phase summary, score breakdown, formula, executive summary, counts, findings, and Phase 1 result.

## Backend Startup And Persistence

Backend app path: `backend/app.py`

Startup flow:

1. FastAPI app is created.
2. CORS middleware reads `CORS_ORIGINS`.
3. On startup, `init_db()` from `backend/db.py` runs.
4. SQLAlchemy creates all ORM tables.
5. Idempotent migrations add email verification and password reset columns to `users`.

Live ORM tables:

| Model | Table | Purpose |
| --- | --- | --- |
| `PendingSignup` | `pending_signups` | Holds unverified signup email, password hash, token hash, and expiry. |
| `User` | `users` | User identity, password hash, active state, verification state, reset tokens. |
| `Project` | `projects` | Groups scans by exact `base_url`. |
| `Scan` | `scans` | Stores scan configuration, status, metrics, timestamps, and raw report. |
| `Finding` | `findings` | Stores persisted scan findings. |
| `Artifact` | `artifacts` | Stores artifact paths such as recordings. |

Persistence flow for scans:

1. `_create_scan()` finds or creates a `Project` by exact target URL.
2. A `Scan` row is created with `status="running"`.
3. Scanner findings become `Finding` rows.
4. Recording paths become `Artifact` rows.
5. Aggregate counts and raw report are stored on the `Scan`.
6. Scan status becomes `completed`, `stopped`, or `failed`.
7. `finished_at` is set.

## Backend API Paths

Public/basic:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Returns backend running message. |
| `GET` | `/health` | Returns `{ "status": "ok" }`. |

Authentication:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/register` | Creates or updates a pending signup and sends verification email. |
| `POST` | `/auth/login` | Validates email/password and returns bearer token. |
| `POST` | `/auth/resend-verification` | Sends a fresh pending signup verification email. |
| `POST` | `/auth/forgot-password` | Sends password reset email if a verified active account exists. |
| `POST` | `/auth/reset-password` | Validates reset token and updates password. |
| `GET` | `/auth/verify-email` | Converts pending signup into verified user and redirects to frontend. |
| `GET` | `/auth/google` | Creates OAuth state and returns Google authorization URL. |
| `GET` | `/auth/google/callback` | Exchanges Google code, verifies identity, creates/updates user, redirects with token. |
| `GET` | `/auth/me` | Returns current authenticated user information. |

Scan/project APIs requiring bearer auth:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/scan` | Runs a non-streaming scan and returns raw report. |
| `POST` | `/scan/live` | Runs a streaming scan through Server-Sent Events. |
| `POST` | `/scan/live/stop` | Stops the active live scan and saves partial results when available. |
| `GET` | `/projects` | Lists persisted projects. |
| `GET` | `/scans` | Lists persisted scan summaries. |
| `GET` | `/scans/{scan_id}` | Returns scan detail, findings, artifacts, score breakdown, and previous scan comparison. |
| `GET` | `/projects/{project_id}/scans` | Lists scans for one project. |

## Authentication Flow

Email registration:

1. Frontend sends email/password to `/auth/register`.
2. Backend validates email format and minimum password length.
3. Backend creates a secure token and SHA-256 token hash.
4. Backend stores pending signup data in `pending_signups`.
5. Backend sends an email with `/auth/verify-email?email=...&token=...`.
6. When the link is opened, backend verifies token hash and expiry.
7. Backend creates a verified `User`, deletes the pending signup, and redirects to `FRONTEND_URL/#verified=1`.

Login:

1. Frontend sends credentials to `/auth/login`.
2. Backend verifies the password with Blowfish `crypt`.
3. Backend rejects unverified accounts.
4. Backend returns a signed HS256 JWT.
5. Frontend stores the token in `localStorage` under `qa_access_token`.
6. Future API calls send `Authorization: Bearer <token>`.

Password reset:

1. Frontend sends email to `/auth/forgot-password`.
2. Backend creates a reset token for verified active users.
3. Backend emails `FRONTEND_URL/#reset_token=...&reset_email=...`.
4. Frontend switches to reset mode.
5. Frontend sends email, token, and new password to `/auth/reset-password`.
6. Backend validates token hash/expiry and updates the password.

Google sign-in:

1. Frontend calls `/auth/google`.
2. Backend returns a Google authorization URL with a short-lived state.
3. Google redirects to `/auth/google/callback`.
4. Backend exchanges the code for an identity token.
5. Backend validates token audience and verified email.
6. Backend creates or updates the user.
7. Backend redirects to `FRONTEND_URL/#oauth_token=<jwt>`.
8. Frontend stores the token and enters the dashboard.

## Live Scan Flow

Frontend path: `frontend/src/App.tsx`

Backend path: `backend/app.py`

1. User enters a target URL, scan mode, and headless preference.
2. Frontend calls `POST /scan/live` with auth token.
3. Backend enforces rate limit of five live scans per user per minute.
4. Backend validates that the target resolves to a public HTTP/HTTPS address.
5. Backend creates a queue for events and a `threading.Event` for cancellation.
6. Backend creates a `Scan` row with `status="running"`.
7. Worker thread runs `Phase1Tester`.
8. Scanner logger pushes `{ type: "log", message }` events into the queue.
9. Backend streams events as `text/event-stream`.
10. Frontend reads stream chunks, parses event payloads, updates live console state, and stores final raw report.
11. On success, backend persists full results and sends `{ type: "done", report, scan_id }`.
12. On stop, backend persists partial results and sends `{ type: "stopped", report, scan_id }`.
13. On error, backend marks scan failed when possible and sends `{ type: "error", message }`.
14. Frontend reloads `/projects` and `/scans`.

Stop flow:

1. User presses Stop Scan.
2. Frontend calls `POST /scan/live/stop`.
3. Backend sets the active scan stop event.
4. Backend attempts to close active Playwright page, context, and browser.
5. Scanner raises `RuntimeError("Scan stopped")`.
6. Backend calls `partial_report()` and persists status `stopped`.

## Frontend UI Workflow

Main file: `frontend/src/App.tsx`

Initial unauthenticated flow:

1. Reads `qa_access_token` from `localStorage`.
2. Parses path and hash for auth mode, verification success, OAuth token, or reset token.
3. Shows login/register/forgot/reset screens when no token exists.

Authenticated dashboard flow:

1. Loads `/projects` and `/scans`.
2. Shows sidebar metrics for project count, scan count, and finding count.
3. Shows overview cards for latest score, average score, passing scans, and projects.
4. Provides scan runner form.
5. Shows live console while scan events stream.
6. Parses completed raw report for summary cards and raw report export.
7. Lists projects and scans with search, status filter, and pagination.

Frontend hash routes:

| Route | View |
| --- | --- |
| `/login` | Login form. |
| `/register` | Registration form. |
| `/forgot-password` | Forgot password form. |
| `/reset-password` | Reset password form. |
| `/#scan/{id}` | Scan detail page. |
| `/#project/{id}` | Project detail page. |
| `/` | Dashboard when authenticated. |

Scan detail page:

1. Fetches `/scans/{id}`.
2. Displays execution details, score, risk, pages tested, total/unique findings, issue counts, findings grouped by category, score breakdown, previous scan comparison, tested pages, and raw report.
3. Allows raw report export as a `.txt` file.

Project detail page:

1. Uses loaded projects/scans to find the selected project.
2. Shows latest score, total scans, average score, total findings, project metadata, latest assessment, and scan history.
3. Allows configuring a new scan with the project base URL.

## Deployment Workflow

Example production proxy path: `deploy/Caddyfile`

1. Run backend locally behind Caddy:

```bash
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

2. Configure Caddy with a real DNS name.
3. Caddy terminates HTTPS, enables gzip, applies security headers, limits request body size to 2 MB, and reverse proxies to `127.0.0.1:8000`.
4. Set production `CORS_ORIGINS` to the exact frontend HTTPS origin.
5. Use a strong `JWT_SECRET`.
6. Configure SMTP and public `BACKEND_URL`/`FRONTEND_URL`.
7. Configure Google OAuth redirect URI when using Google sign-in.
8. Replace the in-memory live scan rate limiter with a shared backend such as Redis before running multiple backend processes.

## Testing Coverage

Test file: `tests/test_phase1_scaffold.py`

Covered behavior:

1. HTTP scanner counts pages, broken links, and findings.
2. Stopped scans retain partial reports.
3. Browser mode can run with mocked Playwright.
4. Browser action navigation restores the original page.
5. Closed browser pages can be reopened.
6. Raw report rendering includes expected sections and severities.
7. Phase summary, risk bands, score formula, and crawl labels render correctly.
8. Unique slow pages affect performance scoring and recommendations.
9. Crawl graph uses friendly labels.

## Important Implementation Notes

1. The live SQLAlchemy schema in `backend/orm_models.py` is the authoritative runtime schema. `qa_schema.sql` appears to be a legacy/reference schema and does not match the current scanner/project model.
2. Projects are grouped by exact `base_url`. Different trailing slash or URL forms may create separate projects unless normalized before submission.
3. `/scan` does not call `_enforce_rate_limit()` or `validate_public_url()` in the current code, while `/scan/live` does. For production, those protections should be aligned.
4. `User.email_verified` defaults to `True` at the ORM level, but the current registration flow creates users only after pending email verification.
5. JWT handling is implemented locally in `backend/security.py` using HS256 HMAC. Production security depends on a strong `JWT_SECRET`.
6. The rate limiter is in memory, so it resets on restart and is not shared across multiple workers.
7. Browser artifacts/screenshots are stored as local temporary file paths. The app persists paths, not uploaded binary files.
8. The frontend parses parts of the raw text report, so changes to `format_raw_report()` can affect dashboard rendering.

