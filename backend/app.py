from __future__ import annotations

import json
import re
import threading
import queue
import sys
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from qa_platform.reporting import format_raw_report  # noqa: E402
from qa_platform.scanner import Phase1Tester  # noqa: E402

from .db import SessionLocal, init_db  # noqa: E402
from .orm_models import Artifact, Finding as ORMFinding, Project, Scan  # noqa: E402


app = FastAPI(
    title="Autonomous QA Backend",
    version="0.1.0",
    description="FastAPI wrapper around the autonomous website testing workflow.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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


def _persist_scan(db: Session, request: ScanRequest, report) -> Scan:
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
    scan.status = "completed"
    scan.finished_at = scan.finished_at or scan.started_at
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


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/scan", response_model=ScanResponse)
def scan(request: ScanRequest) -> ScanResponse:
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


@app.post("/scan/live")
def scan_live(request: ScanLiveRequest) -> StreamingResponse:
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
        report = None
        try:
            browser_mode = "browser" if request.mode == "browser-fast" else request.mode
            report = Phase1Tester(
                str(request.url),
                browser_mode=browser_mode,
                headless=request.headless,
                fast_browser=request.mode == "browser-fast",
                logger=logger,
                stop_event=stop_event,
                runtime_hook=_update_live_scan_runtime,
            ).run()
            _persist_scan(db, request, report)
            db.commit()
            events.put({"type": "done", "report": format_raw_report(report)})
        except Exception as exc:
            db.rollback()
            if str(exc) == "Scan stopped":
                events.put({"type": "stopped", "message": "Scan stopped"})
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


@app.post("/scan/live/stop")
def stop_live_scan() -> dict[str, str]:
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


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Autonomous QA backend is running"}


@app.get("/projects", response_model=list[ProjectResponse])
def list_projects() -> list[ProjectResponse]:
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.created_at.desc()).all()
        return [_project_response(project) for project in projects]
    finally:
        db.close()


@app.get("/scans", response_model=list[ScanSummaryResponse])
def list_scans() -> list[ScanSummaryResponse]:
    db = SessionLocal()
    try:
        scans = db.query(Scan).order_by(Scan.started_at.desc()).all()
        return [_scan_summary(scan) for scan in scans]
    finally:
        db.close()


@app.get("/scans/{scan_id}", response_model=ScanDetailResponse)
def get_scan(scan_id: int) -> ScanDetailResponse:
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


@app.get("/projects/{project_id}/scans", response_model=list[ScanSummaryResponse])
def list_project_scans(project_id: int) -> list[ScanSummaryResponse]:
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        scans = db.query(Scan).filter(Scan.project_id == project_id).order_by(Scan.started_at.desc()).all()
        return [_scan_summary(scan) for scan in scans]
    finally:
        db.close()
