# Autonomous QA Platform

Autonomous QA Platform is a Phase 1 website testing stack with:

- a Python CLI scanner in `src/qa_platform`
- a FastAPI backend in `backend/`
- a React/Vite frontend in `frontend/`

The scanner can use Playwright when it is installed. If Playwright is not available, it falls back to HTTP crawling.

## Project Layout

- `src/qa_platform/` - core scanning and report formatting logic
- `backend/` - FastAPI app, database models, and API endpoints
- `frontend/` - React dashboard for scan and project data
- `tests/` - scaffold tests
- `.env.example` - sample environment variables
- `qa_schema.sql` - database schema

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

4. Copy `.env.example` to `.env` and adjust any local settings you need.

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

The API initializes the database on startup and accepts requests from the local frontend on `http://localhost:5173`.

Main endpoints:

- `GET /health`
- `GET /`
- `POST /scan`

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
npm install
npm run dev
```

Build for production:

```bash
npm run build
```

## Environment

Use `.env.example` as the template for local configuration.

## Contributing

- Keep the CLI, backend, and frontend instructions in sync with the actual repo layout.
- Update `.gitignore` when new generated files or local artifacts are introduced.
- Prefer small, verifiable documentation changes when behavior does not change.
