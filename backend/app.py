from __future__ import annotations

import json
from datetime import datetime, timedelta
import re
import threading
import queue
import time
import os
import secrets
from urllib.parse import urlencode, urlparse
from urllib.request import Request as URLRequest, urlopen
import sys
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from qa_platform.reporting import format_raw_report  # noqa: E402
from qa_platform.scanner import Phase1Tester  # noqa: E402

from .db import SessionLocal, init_db  # noqa: E402
from .orm_models import Artifact, Finding as ORMFinding, PendingSignup, Project, Scan, User  # noqa: E402
from .security import create_access_token, get_current_user, hash_password, validate_public_url, verify_password  # noqa: E402
from .email_service import PASSWORD_RESET_TTL_HOURS, create_verification_token, hash_verification_token, send_password_reset_email, send_verification_email  # noqa: E402



app = FastAPI(
    title="Autonomous QA Backend",
    version="0.1.0",
    description="FastAPI wrapper around the autonomous website testing workflow.",
    openapi_tags=[
        {"name": "health", "description": "Service status and basic backend availability checks."},
        {"name": "auth", "description": "Email/password account registration, login, verification, and password reset."},
        {"name": "oauth", "description": "Google OAuth sign-in and callback handling."},
        {"name": "scan", "description": "Website scan execution and live scan control."},
        {"name": "projects", "description": "Project records grouped by scanned base URL."},
        {"name": "scan-results", "description": "Saved scan summaries, details, findings, artifacts, and comparisons."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_live_scan_lock = threading.Lock()
_live_scan_state: dict[str, object] = {
    "stop_event": None,
    "active": False,
    "browser": None,
    "context": None,
    "page": None,
}
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict[tuple[int, str], list[float]] = {}
_google_states: dict[str, float] = {}


def _enforce_rate_limit(user: User, bucket: str, limit: int = 5, window_seconds: int = 60) -> None:
    now = time.monotonic()
    key = (user.id, bucket)
    with _rate_limit_lock:
        hits = [stamp for stamp in _rate_limit_hits.get(key, []) if now - stamp < window_seconds]
        if len(hits) >= limit:
            retry_after = max(1, int(window_seconds - (now - hits[0])))
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Try again in {retry_after} seconds.", headers={"Retry-After": str(retry_after)})
        hits.append(now)
        _rate_limit_hits[key] = hits


def _update_live_scan_runtime(**kwargs) -> None:
    with _live_scan_lock:
        for key, value in kwargs.items():
          _live_scan_state[key] = value


@app.on_event("startup")
def on_startup() -> None:
    init_db()


class ScanRequest(BaseModel):
    url: HttpUrl
    mode: Literal["auto", "browser", "browser-fast", "http"] = "auto"
    headless: bool = False


class ScanResponse(BaseModel):
    report: str


class ScanLiveRequest(ScanRequest):
    pass


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirmRequest(BaseModel):
    email: str
    token: str
    password: str


class EmailRequest(BaseModel):
    email: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str


class MessageResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str


class ProjectResponse(BaseModel):
    id: int
    name: str
    base_url: str
    created_at: str
    updated_at: str


class FindingResponse(BaseModel):
    id: int
    category: str
    message: str
    url: str | None
    note: str | None
    created_at: str


class ArtifactResponse(BaseModel):
    id: int
    kind: str
    path: str
    created_at: str


class ScanSummaryResponse(BaseModel):
    id: int
    project_id: int | None
    url: str
    mode: str
    headless: bool
    status: str
    started_at: str
    finished_at: str | None
    pages_tested: int
    broken_links: int
    js_errors: int
    api_failures: int
    resource_failures: int
    third_party_failures: int
    navigation_failures: int
    missing_elements: int
    slow_pages: int
    total_findings: int
    site_score: int
    risk_level: str
    unique_findings: int
    phase2_summary: str | None = None
    executive_summary: str | None = None


class ScoreBreakdownItem(BaseModel):
    label: str
    score: int
    deductions: list[str]


class ScanComparisonResponse(BaseModel):
    previous_scan_id: int | None
    previous_score: int | None
    score_delta: int | None
    previous_risk_level: str | None
    comparison_note: str | None


class ScanDetailResponse(ScanSummaryResponse):
    raw_report: str | None
    findings: list[FindingResponse]
    artifacts: list[ArtifactResponse]
    score_breakdown: list[ScoreBreakdownItem]
    phase2_summary: str | None
    comparison: ScanComparisonResponse | None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_raw_report(raw_report: str | None) -> dict[str, object]:
    data: dict[str, object] = {
        "site_score": 0,
        "risk_level": "Unknown",
        "unique_findings": 0,
        "phase2_summary": None,
        "executive_summary": None,
        "score_breakdown": [],
    }
    if not raw_report:
        return data
    score_match = re.search(r"^Site Score:\s*(\d+)/100$", raw_report, re.MULTILINE)
    risk_match = re.search(r"^Risk Level:\s*(.+)$", raw_report, re.MULTILINE)
    unique_match = re.search(r"^Unique Findings:\s*(\d+)$", raw_report, re.MULTILINE)
    if score_match:
        data["site_score"] = int(score_match.group(1))
    if risk_match:
        data["risk_level"] = risk_match.group(1).strip()
    if unique_match:
        data["unique_findings"] = int(unique_match.group(1))
    if "Phase 2 Summary:" in raw_report:
        phase2_lines = []
        capture = False
        for line in raw_report.splitlines():
            if line == "Phase 2 Summary:":
                capture = True
                continue
            if capture and line.startswith("Score Breakdown:"):
                break
            if capture and line.startswith("Site Score:"):
                break
            if capture and line.startswith("- "):
                phase2_lines.append(line[2:].strip())
        if phase2_lines:
            data["phase2_summary"] = " ".join(phase2_lines)
    if "Executive Summary:" in raw_report:
        executive_lines = []
        capture = False
        for line in raw_report.splitlines():
            if line == "Executive Summary:":
                capture = True
                continue
            if capture and line.startswith("Site Score:"):
                break
            if capture and line.startswith("- "):
                executive_lines.append(line[2:].strip())
        if executive_lines:
            data["executive_summary"] = "\n".join(executive_lines)
    breakdown: list[ScoreBreakdownItem] = []
    current_label = None
    current_score = None
    current_deductions: list[str] = []
    for line in raw_report.splitlines():
        if line == "Score Breakdown:":
            current_label = None
            current_score = None
            current_deductions = []
            continue
        if line.startswith("- ") and "/100" in line and ":" in line:
            if current_label is not None:
                breakdown.append(
                    ScoreBreakdownItem(label=current_label, score=current_score or 0, deductions=current_deductions)
                )
            label_part, score_part = line[2:].split(":", 1)
            current_label = label_part.strip()
            current_score = int(score_part.strip().split("/", 1)[0])
            current_deductions = []
            continue
        if line.startswith("  ") and current_label is not None:
            current_deductions.append(line.strip())
    if current_label is not None:
        breakdown.append(ScoreBreakdownItem(label=current_label, score=current_score or 0, deductions=current_deductions))
    data["score_breakdown"] = breakdown
    return data

def _create_scan(db: Session, request: ScanRequest) -> Scan:
    project = db.query(Project).filter(Project.base_url == str(request.url)).one_or_none()
    if project is None:
        project = Project(name=str(request.url), base_url=str(request.url))
        db.add(project)
        db.flush()

    scan = Scan(
        project_id=project.id,
        url=str(request.url),
        mode=request.mode,
        headless=request.headless,
        status="running",
    )
    db.add(scan)
    db.flush()
    return scan


def _persist_scan(
    db: Session,
    request: ScanRequest,
    report,
    *,
    scan: Scan | None = None,
    status: str = "completed",
) -> Scan:
    scan = scan or _create_scan(db, request)

    for finding in report.findings:
        note = None
        if finding.evidence:
            parts = []
            for item in finding.evidence:
                parts.append(f"{item.kind}:{item.value}")
                if item.note:
                    parts.append(f"note={item.note}")
                if item.screenshot_path:
                    parts.append(f"screenshot={item.screenshot_path}")
                if item.recording_path:
                    parts.append(f"recording={item.recording_path}")
            note = "; ".join(parts)
        db.add(
            ORMFinding(
                scan_id=scan.id,
                category=finding.category,
                message=finding.message,
                url=finding.url,
                note=note,
            )
        )

    for path in report.recordings:
        db.add(Artifact(scan_id=scan.id, kind="recording", path=path))

    scan.pages_tested = report.pages_tested
    scan.broken_links = report.broken_links
    scan.js_errors = report.js_errors
    scan.api_failures = report.api_failures
    scan.resource_failures = report.resource_failures
    scan.third_party_failures = report.third_party_failures
    scan.navigation_failures = report.navigation_failures
    scan.missing_elements = report.missing_elements
    scan.slow_pages = report.slow_pages
    scan.total_findings = report.total_findings
    scan.raw_report = format_raw_report(report)
    scan.status = status
    scan.finished_at = datetime.utcnow()
    return scan


def _scan_summary(scan: Scan) -> ScanSummaryResponse:
    parsed = _parse_raw_report(scan.raw_report)
    return ScanSummaryResponse(
        id=scan.id,
        project_id=scan.project_id,
        url=scan.url,
        mode=scan.mode,
        headless=scan.headless,
        status=scan.status,
        started_at=_iso(scan.started_at) or "",
        finished_at=_iso(scan.finished_at),
        pages_tested=scan.pages_tested,
        broken_links=scan.broken_links,
        js_errors=scan.js_errors,
        api_failures=scan.api_failures,
        resource_failures=scan.resource_failures,
        third_party_failures=scan.third_party_failures,
        navigation_failures=scan.navigation_failures,
        missing_elements=scan.missing_elements,
        slow_pages=scan.slow_pages,
        total_findings=scan.total_findings,
        site_score=int(parsed["site_score"]),
        risk_level=str(parsed["risk_level"]),
        unique_findings=int(parsed["unique_findings"]),
        phase2_summary=str(parsed["phase2_summary"]) if parsed["phase2_summary"] else None,
        executive_summary=str(parsed["executive_summary"]) if parsed["executive_summary"] else None,
    )


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        base_url=project.base_url,
        created_at=_iso(project.created_at) or "",
        updated_at=_iso(project.updated_at) or "",
    )


def _finding_response(finding: ORMFinding) -> FindingResponse:
    return FindingResponse(
        id=finding.id,
        category=finding.category,
        message=finding.message,
        url=finding.url,
        note=finding.note,
        created_at=_iso(finding.created_at) or "",
    )


def _artifact_response(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=artifact.id,
        kind=artifact.kind,
        path=artifact.path,
        created_at=_iso(artifact.created_at) or "",
    )


def _comparison_response(db: Session, scan: Scan, current_score: int, current_risk_level: str) -> ScanComparisonResponse | None:
    if scan.project_id is None:
        return None
    previous = (
        db.query(Scan)
        .filter(Scan.project_id == scan.project_id, Scan.id < scan.id)
        .order_by(Scan.id.desc())
        .first()
    )
    if previous is None:
        return None
    previous_parsed = _parse_raw_report(previous.raw_report)
    previous_score = int(previous_parsed["site_score"])
    delta = current_score - previous_score if previous_score is not None else None
    note = None
    if delta is not None:
        if delta > 0:
            note = "Improved compared with the previous scan."
        elif delta < 0:
            note = "Regressed compared with the previous scan."
        else:
            note = "No score change compared with the previous scan."
    return ScanComparisonResponse(
        previous_scan_id=previous.id,
        previous_score=previous_score,
        score_delta=delta,
        previous_risk_level=str(previous_parsed["risk_level"]),
        comparison_note=note,
    )


@app.get("/health", response_model=HealthResponse, tags=["health"], summary="Check backend health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/scan", response_model=ScanResponse, tags=["scan"], summary="Run a non-streaming website scan")
def scan(request: ScanRequest, user: User = Depends(get_current_user)) -> ScanResponse:
    browser_mode = "browser" if request.mode == "browser-fast" else request.mode
    db = SessionLocal()
    try:
        report = Phase1Tester(
            str(request.url),
            browser_mode=browser_mode,
            headless=request.headless,
            fast_browser=request.mode == "browser-fast",
        ).run()
        _persist_scan(db, request, report)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        db.close()
    return ScanResponse(report=format_raw_report(report))


@app.post("/scan/live", tags=["scan"], summary="Run a live streaming website scan")
def scan_live(request: ScanLiveRequest, user: User = Depends(get_current_user)) -> StreamingResponse:
    _enforce_rate_limit(user, "scan_live")
    validate_public_url(str(request.url))
    events: queue.Queue[object] = queue.Queue()
    done = object()
    stop_event = threading.Event()

    with _live_scan_lock:
        _live_scan_state["stop_event"] = stop_event
        _live_scan_state["active"] = True
        _live_scan_state["browser"] = None
        _live_scan_state["context"] = None
        _live_scan_state["page"] = None

    def logger(message: str) -> None:
        events.put({"type": "log", "message": message})

    def worker() -> None:
        db = SessionLocal()
        tester = None
        scan_id = None
        try:
            scan_record = _create_scan(db, request)
            db.commit()
            scan_id = scan_record.id

            browser_mode = "browser" if request.mode == "browser-fast" else request.mode
            tester = Phase1Tester(
                str(request.url),
                browser_mode=browser_mode,
                headless=request.headless,
                fast_browser=request.mode == "browser-fast",
                logger=logger,
                stop_event=stop_event,
                runtime_hook=_update_live_scan_runtime,
            )
            report = tester.run()
            scan_record = db.get(Scan, scan_id)
            _persist_scan(db, request, report, scan=scan_record, status="completed")
            db.commit()
            events.put({"type": "done", "report": format_raw_report(report), "scan_id": scan_id})
        except Exception as exc:
            db.rollback()
            stopped = stop_event.is_set() or str(exc) == "Scan stopped"
            partial_report = tester.partial_report() if tester is not None else None
            try:
                scan_record = db.get(Scan, scan_id) if scan_id is not None else None
                if scan_record is not None and partial_report is not None:
                    _persist_scan(
                        db,
                        request,
                        partial_report,
                        scan=scan_record,
                        status="stopped" if stopped else "failed",
                    )
                elif scan_record is not None:
                    scan_record.status = "stopped" if stopped else "failed"
                    scan_record.finished_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()

            if stopped:
                events.put(
                    {
                        "type": "stopped",
                        "message": "Scan stopped — partial results saved",
                        "report": format_raw_report(partial_report) if partial_report is not None else "",
                        "scan_id": scan_id,
                    }
                )
            else:
                events.put({"type": "error", "message": str(exc)})
        finally:
            db.close()
            with _live_scan_lock:
                _live_scan_state["active"] = False
                _live_scan_state["stop_event"] = None
                _live_scan_state["browser"] = None
                _live_scan_state["context"] = None
                _live_scan_state["page"] = None
            events.put(done)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def stream():
        while True:
            item = events.get()
            if item is done:
                yield "event: done\ndata: {}\n\n"
                break
            payload = json.dumps(item)
            event_type = item.get("type", "log") if isinstance(item, dict) else "log"
            yield f"event: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/scan/live/stop", tags=["scan"], summary="Stop the active live scan")
def stop_live_scan(user: User = Depends(get_current_user)) -> dict[str, str]:
    with _live_scan_lock:
        stop_event = _live_scan_state.get("stop_event")
        active = bool(_live_scan_state.get("active"))
        browser = _live_scan_state.get("browser")
        context = _live_scan_state.get("context")
        page = _live_scan_state.get("page")
    if not active or not isinstance(stop_event, threading.Event):
        raise HTTPException(status_code=409, detail="No active scan to stop")
    stop_event.set()
    for handle in (page, context, browser):
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
    return {"status": "stopping"}


@app.post("/auth/register", response_model=MessageResponse, tags=["auth"], summary="Register with email and send activation email")
def register(request: RegisterRequest) -> MessageResponse:
    email = request.email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    token, token_hash, expires_at = create_verification_token()
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).one_or_none()
        if existing is not None:
            if existing.email_verified:
                raise HTTPException(status_code=409, detail="An account with that email already exists")
            # Migrate an account created by the earlier flow into pending signup.
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).one_or_none()
            if pending is None:
                pending = PendingSignup(email=email, password_hash=existing.password_hash, verification_token_hash=token_hash, verification_expires_at=expires_at)
                db.add(pending)
            else:
                pending.password_hash = existing.password_hash
                pending.verification_token_hash = token_hash
                pending.verification_expires_at = expires_at
            db.delete(existing)
        else:
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).one_or_none()
            if pending is None:
                pending = PendingSignup(email=email, password_hash=hash_password(request.password), verification_token_hash=token_hash, verification_expires_at=expires_at)
                db.add(pending)
            else:
                pending.password_hash = hash_password(request.password)
                pending.verification_token_hash = token_hash
                pending.verification_expires_at = expires_at
        db.commit()
        try:
            send_verification_email(email, token)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Verification email could not be sent: {exc}")
        return MessageResponse(message="Verification email sent. Your account will be created after confirmation.")
    finally:
        db.close()


@app.post("/auth/login", response_model=AuthResponse, tags=["auth"], summary="Sign in with email and password")
def login(request: LoginRequest) -> AuthResponse:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == request.email.strip().lower()).one_or_none()
        if user is None or not verify_password(request.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user.email_verified:
            raise HTTPException(status_code=403, detail="Please verify your email before signing in")
        return AuthResponse(access_token=create_access_token(user), user_id=user.id, email=user.email)
    finally:
        db.close()


@app.post("/auth/resend-verification", response_model=MessageResponse, tags=["auth"], summary="Resend account activation email")
def resend_verification(request: EmailRequest) -> MessageResponse:
    email = request.email.strip().lower()
    db = SessionLocal()
    try:
        pending = db.query(PendingSignup).filter(PendingSignup.email == email).one_or_none()
        if pending is None:
            return MessageResponse(message="If that email has a pending signup, a new verification email has been sent.")
        token, token_hash, expires_at = create_verification_token()
        pending.verification_token_hash = token_hash
        pending.verification_expires_at = expires_at
        db.commit()
        try:
            send_verification_email(email, token)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Verification email could not be sent: {exc}")
        return MessageResponse(message="If that email has a pending signup, a new verification email has been sent.")
    finally:
        db.close()


@app.post("/auth/forgot-password", response_model=MessageResponse, tags=["auth"], summary="Send password reset email")
def forgot_password(request: PasswordResetRequest) -> MessageResponse:
    email = request.email.strip().lower()
    message = "If an account exists for that email, a password reset link has been sent."
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email, User.is_active.is_(True), User.email_verified.is_(True)).one_or_none()
        if user is None:
            return MessageResponse(message=message)
        token, token_hash, _ = create_verification_token()
        user.password_reset_token_hash = token_hash
        user.password_reset_expires_at = datetime.utcnow() + timedelta(hours=PASSWORD_RESET_TTL_HOURS)
        db.commit()
        try:
            send_password_reset_email(email, token)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Password reset email could not be sent: {exc}")
        return MessageResponse(message=message)
    finally:
        db.close()


@app.post("/auth/reset-password", response_model=MessageResponse, tags=["auth"], summary="Reset password with token")
def reset_password(request: PasswordResetConfirmRequest) -> MessageResponse:
    email = request.email.strip().lower()
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email, User.is_active.is_(True)).one_or_none()
        now = datetime.utcnow()
        if user is None or user.password_reset_expires_at is None or user.password_reset_expires_at < now or user.password_reset_token_hash != hash_verification_token(request.token):
            raise HTTPException(status_code=400, detail="This password reset link is invalid or expired")
        user.password_hash = hash_password(request.password)
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        db.commit()
        return MessageResponse(message="Your password has been reset. You can now sign in.")
    finally:
        db.close()


@app.get("/auth/verify-email", tags=["auth"], summary="Activate account from verification email")
def verify_email(email: str, token: str):
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    db = SessionLocal()
    try:
        pending = db.query(PendingSignup).filter(PendingSignup.email == email.strip().lower()).one_or_none()
        now = datetime.utcnow()
        if pending is None or pending.verification_expires_at < now or pending.verification_token_hash != hash_verification_token(token):
            raise HTTPException(status_code=400, detail="This verification link is invalid or expired")
        user = User(email=pending.email, password_hash=pending.password_hash, email_verified=True)
        db.add(user)
        db.delete(pending)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"{frontend_url}/#verified=1")


@app.get("/auth/google", tags=["oauth"], summary="Create Google sign-in URL")
def google_login() -> dict[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback").strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state = secrets.token_urlsafe(32)
    _google_states[state] = time.time() + 600
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "state": state,
        "prompt": "select_account",
    })
    return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


@app.get("/auth/google/callback", tags=["oauth"], summary="Handle Google OAuth callback")
def google_callback(code: str, state: str):
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    expires_at = _google_states.pop(state, 0)
    if not expires_at or expires_at < time.time():
        raise HTTPException(status_code=400, detail="Invalid or expired Google sign-in state")
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    token_request = URLRequest(
        "https://oauth2.googleapis.com/token",
        data=urlencode({"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(token_request, timeout=10) as response:
            token_payload = json.loads(response.read().decode())
        id_token = token_payload.get("id_token")
        if not id_token:
            raise ValueError("Google did not return an identity token")
        with urlopen(f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}", timeout=10) as response:
            profile = json.loads(response.read().decode())
        if profile.get("aud") != client_id or profile.get("email_verified") != "true":
            raise ValueError("Google identity could not be verified")
        email = str(profile.get("email", "")).strip().lower()
        if not email:
            raise ValueError("Google account has no verified email")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Google sign-in failed: {exc}")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one_or_none()
        if user is None:
            user = User(email=email, password_hash=hash_password(secrets.token_urlsafe(32)), email_verified=True)
            db.add(user)
        elif not user.email_verified:
            user.email_verified = True
            user.verification_token_hash = None
            user.verification_expires_at = None
        db.commit()
        db.refresh(user)
        token = create_access_token(user)
    finally:
        db.close()
    return RedirectResponse(f"{frontend_url}/#oauth_token={token}")


@app.get("/auth/me", response_model=AuthResponse, tags=["auth"], summary="Get current authenticated user")
def auth_me(user: User = Depends(get_current_user)) -> AuthResponse:
    return AuthResponse(access_token="", user_id=user.id, email=user.email)


@app.get("/", tags=["health"], summary="Backend running message")
def root() -> dict[str, str]:
    return {"message": "Autonomous QA backend is running"}


@app.get("/projects", response_model=list[ProjectResponse], tags=["projects"], summary="List projects")
def list_projects(user: User = Depends(get_current_user)) -> list[ProjectResponse]:
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.created_at.asc()).all()
        return [_project_response(project) for project in projects]
    finally:
        db.close()


@app.get("/scans", response_model=list[ScanSummaryResponse], tags=["scan-results"], summary="List scan summaries")
def list_scans(user: User = Depends(get_current_user)) -> list[ScanSummaryResponse]:
    db = SessionLocal()
    try:
        scans = db.query(Scan).order_by(Scan.started_at.asc()).all()
        return [_scan_summary(scan) for scan in scans]
    finally:
        db.close()


@app.get("/scans/{scan_id}", response_model=ScanDetailResponse, tags=["scan-results"], summary="Get scan detail")
def get_scan(scan_id: int, user: User = Depends(get_current_user)) -> ScanDetailResponse:
    db = SessionLocal()
    try:
        scan = db.query(Scan).filter(Scan.id == scan_id).one_or_none()
        if scan is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        parsed = _parse_raw_report(scan.raw_report)
        summary = _scan_summary(scan).model_dump()
        summary["phase2_summary"] = str(parsed["phase2_summary"]) if parsed["phase2_summary"] else summary.get("phase2_summary")
        summary["executive_summary"] = str(parsed["executive_summary"]) if parsed["executive_summary"] else summary.get("executive_summary")
        return ScanDetailResponse(
            **summary,
            raw_report=scan.raw_report,
            findings=[_finding_response(finding) for finding in scan.findings],
            artifacts=[_artifact_response(artifact) for artifact in scan.artifacts],
            score_breakdown=list(parsed["score_breakdown"]),
            comparison=_comparison_response(db, scan, int(parsed["site_score"]), str(parsed["risk_level"])),
        )
    finally:
        db.close()


@app.get("/projects/{project_id}/scans", response_model=list[ScanSummaryResponse], tags=["projects", "scan-results"], summary="List scans for a project")
def list_project_scans(project_id: int, user: User = Depends(get_current_user)) -> list[ScanSummaryResponse]:
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        scans = db.query(Scan).filter(Scan.project_id == project_id).order_by(Scan.started_at.asc()).all()
        return [_scan_summary(scan) for scan in scans]
    finally:
        db.close()
