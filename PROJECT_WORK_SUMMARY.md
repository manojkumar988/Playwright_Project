# Project And Work Summary

## Project Overview

Autonomous QA Platform is a website testing app made of three parts:

- `src/qa_platform/`: Python scanner and CLI.
- `backend/`: FastAPI API, auth, database, email, scan orchestration, and persistence.
- `frontend/`: React/Vite dashboard for login, signup, scan runner, live console, projects, scans, findings, and reports.

The main purpose of the product is simple: the user gives a website URL, the system scans it, finds quality issues, scores the site, saves results, and shows everything in the dashboard.

## Main URL-To-Scan Workflow

This is the core workflow of the project:

1. User logs in and enters a website URL in the frontend scan runner.
2. Frontend normalizes the URL and sends it to `POST /scan/live` with scan mode and headless setting.
3. Backend verifies the user token and applies live scan rate limiting.
4. Backend validates that the target is a public HTTP/HTTPS URL and blocks private/internal/local addresses.
5. Backend finds or creates a `Project` for that base URL.
6. Backend creates a `Scan` row with `status="running"`.
7. Backend starts `Phase1Tester` from `src/qa_platform/scanner.py` in a worker thread.
8. Scanner starts crawling/testing the site and sends live log events back through the backend.
9. Backend streams those events to the frontend as `text/event-stream`.
10. Frontend displays live progress in the scan console.
11. Scanner builds a final `TestReport` with pages tested, clicked URLs, findings, unique findings, evidence, recordings, score, risk level, and summaries.
12. Backend saves metrics, findings, artifacts, raw report, final status, and finish time.
13. Frontend refreshes `/projects` and `/scans`, then shows the completed report and scan detail.

## What The Scanner Does

The main scanner class is `Phase1Tester` in `src/qa_platform/scanner.py`.

Supported scan modes:

- `auto`: browser scan when Playwright is available, otherwise HTTP crawl.
- `browser`: Playwright Chromium scan.
- `browser-fast`: browser scan with reduced scrolling/action exploration.
- `http`: HTTP crawl using standard Python HTTP tooling.

During scanning, the system can:

- Normalize the target URL.
- Stay on the same host.
- Crawl pages up to the configured limit.
- Collect anchor links, navigation links, DOM/data/form links, and visible clickable links.
- Detect broken links, navigation failures, JavaScript errors, API failures, resource failures, third-party failures, slow pages, and missing-content signals.
- Dismiss common popups/cookie banners in browser mode.
- Scroll pages and exercise visible actions in browser mode.
- Capture evidence and browser recording paths when available.
- Deduplicate findings.
- Calculate score breakdown, site score, risk level, phase summary, and executive summary.

## Live Scan Stop Flow

If the user clicks `Stop Scan`:

1. Frontend calls `POST /scan/live/stop`.
2. Backend sets the active stop event.
3. Backend tries to close the active Playwright page, context, and browser.
4. Scanner raises `RuntimeError("Scan stopped")`.
5. Backend saves a partial report with `status="stopped"` when partial results exist.
6. Frontend updates the live console and refreshes projects/scans.

## Dashboard Result Flow

After a scan, the dashboard can show:

- Latest site score and risk level.
- Pages tested.
- Total and unique findings.
- Findings grouped by category.
- Issue counts by type.
- Score breakdown.
- Previous scan comparison for the same project.
- Tested page list.
- Raw report export.
- Project history, average score, and latest scan summary.

## API Groups And Tags

Related APIs are grouped by feature area so the workflow is easier to understand.

### Health APIs

| Tag | Method | API | Purpose |
| --- | --- | --- | --- |
| `health` | `GET` | `/` | Confirms the backend is running. |
| `health` | `GET` | `/health` | Returns backend health status. |

### Auth APIs

| Tag | Method | API | Purpose |
| --- | --- | --- | --- |
| `auth` | `POST` | `/auth/register` | Starts email/password signup and sends activation email. |
| `auth` | `POST` | `/auth/login` | Logs in with email/password and returns JWT token. |
| `auth` | `POST` | `/auth/resend-verification` | Sends a fresh activation email for pending signup. |
| `auth` | `POST` | `/auth/forgot-password` | Sends password reset email for verified active users. |
| `auth` | `POST` | `/auth/reset-password` | Validates reset token and updates password. |
| `auth` | `GET` | `/auth/verify-email` | Activates account from email verification link. |
| `auth` | `GET` | `/auth/me` | Returns current authenticated user. |

### Google Auth APIs

| Tag | Method | API | Purpose |
| --- | --- | --- | --- |
| `oauth` | `GET` | `/auth/google` | Creates Google OAuth URL. |
| `oauth` | `GET` | `/auth/google/callback` | Handles Google callback and redirects with app token. |

### Scan APIs

| Tag | Method | API | Purpose |
| --- | --- | --- | --- |
| `scan` | `POST` | `/scan` | Runs non-streaming scan and returns raw report. |
| `scan-live` | `POST` | `/scan/live` | Runs live scan and streams progress events. |
| `scan-live` | `POST` | `/scan/live/stop` | Stops active live scan and saves partial results. |
| `scan-result` | `GET` | `/scans` | Lists saved scan summaries. |
| `scan-result` | `GET` | `/scans/{scan_id}` | Shows full scan detail with findings, artifacts, score, and comparison. |

### Project APIs

| Tag | Method | API | Purpose |
| --- | --- | --- | --- |
| `project` | `GET` | `/projects` | Lists saved projects grouped by base URL. |
| `project` | `GET` | `/projects/{project_id}/scans` | Lists scans for one project. |

## Authentication And Email Flow

The app supports email/password signup, login, email verification, forgot password, reset password, resend verification, and Google sign-in. Email/password users verify email before signing in. Password reset links go to verified active users.

Important auth files:

- `backend/app.py`: auth endpoints and scan/project API routes.
- `backend/email_service.py`: verification token creation, token hashing, password reset token support, and outbound email content/delivery.
- `backend/security.py`: password hashing, JWT handling, user lookup, and URL safety validation.
- `backend/orm_models.py`: user and pending signup database tables.
- `frontend/src/App.tsx`: auth screens, reset-token handling, dashboard, scans, and projects.

## Email Improvement Work Completed

The original password reset and account activation emails were plain text and showed raw links directly. We updated `backend/email_service.py` so both email types now send professional multipart emails.

What changed:

- Added HTML email layout with branded `Autonomous QA` header.
- Added CTA buttons: `Activate account` and `Reset password`.
- Added clear purpose labels: `Account activation` and `Password reset`.
- Added expiry details and security copy.
- Added fallback visible link for email clients where buttons fail.
- Kept plain-text fallback for non-HTML clients.
- Escaped dynamic HTML values.
- Centralized SMTP config in `_smtp_config()`.
- Centralized sending in `_send_message()`.
- Preserved existing token, URL, and backend behavior.

## Email Link Formats

Forgot-password email link:

```text
<FRONTEND_URL>/#reset_token=<token>&reset_email=<email>
```

Account activation email link:

```text
<BACKEND_URL>/auth/verify-email?email=<email>&token=<token>
```

## Required Email Environment Variables

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_USE_TLS`
- `BACKEND_URL`
- `FRONTEND_URL`

No database schema change was needed for the email design update.

## Verification Done

Completed checks:

```bash
python3 -m py_compile backend/email_service.py
```

An offline MIME smoke test confirmed both account activation and password reset emails include:

- `text/plain`
- `text/html`

Full test suite status:

- `pytest -q` could not run because `pytest` is not installed.
- `python3 -m pytest -q` also could not run because the `pytest` module is missing.

## Files Changed

- `backend/email_service.py`
- `PROJECT_WORK_SUMMARY.md`

The main product workflow is the URL scan workflow. The recent code change specifically improved the professional email experience for account activation and forgot-password/reset-password flows.
