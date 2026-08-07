# Autonomous QA Platform

Autonomous QA Platform is a website testing stack with:

- a Python CLI scanner in `src/qa_platform`
- a FastAPI backend in `backend/`
- a React/Vite frontend in `frontend/`

The scanner uses Playwright for browser scans when installed and can fall back to HTTP crawling.

## Project Layout

- `src/qa_platform/` - core scanning and report formatting logic
- `backend/` - FastAPI app, database models, and API endpoints
- `frontend/` - React dashboard for scan and project data
- `tests/` - scanner regression tests
- `.env.example` - backend environment template
- `frontend/.env.example` - frontend environment template
- `qa_schema.sql` - database schema reference

## Setup

1. Create and activate a Python virtual environment.
2. Install the Python package in editable mode:

```bash
pip install -e .
```

3. Install frontend dependencies:

```bash
cd frontend
npm install
```

4. Configure the environment files:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

Set `DATABASE_URL` for the backend and `VITE_API_BASE` if the frontend should use a backend other than `http://127.0.0.1:8000`.

## Python CLI

Run a scan from the command line:

```bash
python -m qa_platform https://example.com
```

Options:

- `--mode auto|browser|browser-fast|http`
- `--headless`

Example:

```bash
python -m qa_platform https://example.com --mode browser --headless
```

## FastAPI Backend

Start the backend API:

```bash
uvicorn backend.app:app --reload
```

The API initializes the database on startup and accepts requests from the local frontend.

Main endpoints:

- `GET /health`
- `GET /`
- `POST /scan`
- `POST /scan/live` - streams scan events and the final report
- `POST /scan/live/stop` - stops the active scan and preserves partial results

Example request:

```json
{
  "url": "https://www.think41.com/",
  "mode": "browser",
  "headless": false
}
```

## Frontend

Run the frontend locally:

```bash
cd frontend
npm run dev
```

Build for production:

```bash
npm run build
```

## Contributing

- Keep the CLI, backend, and frontend instructions in sync with the actual repo layout.
- Update `.gitignore` when new generated files or local artifacts are introduced.
- Prefer small, verifiable documentation changes when behavior does not change.

## Security and production deployment

Authentication is enabled for the dashboard and scan/project APIs. Create an account from the frontend, then confirm the verification link sent by SMTP before signing in. Set a strong `JWT_SECRET` in `.env`; the development fallback must not be used in production. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM` for email confirmation. Set `BACKEND_URL` to the public backend address used in confirmation links. Google sign-ins are treated as verified. Scan endpoints are limited to five requests per user per minute, and scan URLs plus redirect destinations are checked against private and local network ranges.

For HTTPS deployment, use the example `deploy/Caddyfile` with a real DNS name. Caddy obtains and renews TLS certificates automatically and proxies to the local Uvicorn process:

```bash
uvicorn backend.app:app --host 127.0.0.1 --port 8000
caddy run --config deploy/Caddyfile
```

Set `CORS_ORIGINS` to the exact HTTPS frontend origin in production. For Google sign-in, create a Google OAuth web client and add `GOOGLE_REDIRECT_URI` as an authorized redirect URI, then set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `FRONTEND_URL`. For multiple backend processes, replace the in-memory rate limiter with Redis-backed limiting.
