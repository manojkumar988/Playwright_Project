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
