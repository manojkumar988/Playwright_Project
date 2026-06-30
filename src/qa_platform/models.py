from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class Evidence:
    kind: str
    value: str
    screenshot_path: Optional[str] = None
    recording_path: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class Finding:
    category: str
    message: str
    evidence: List[Evidence] = field(default_factory=list)
    url: Optional[str] = None


@dataclass(frozen=True)
class PageSummary:
    url: str
    title: str = ""
    status: Optional[int] = None
    response_time_seconds: float = 0.0
    parent_url: Optional[str] = None
    discovered_from: Optional[str] = None
    duplicate_of: Optional[str] = None


@dataclass
class TestReport:
    target_url: str
    pages_tested: int = 0
    tested_urls: List[str] = field(default_factory=list)
    clicked_urls: List[str] = field(default_factory=list)
    page_summaries: List[PageSummary] = field(default_factory=list)
    recordings: List[str] = field(default_factory=list)
    broken_links: int = 0
    js_errors: int = 0
    api_failures: int = 0
    resource_failures: int = 0
    third_party_failures: int = 0
    navigation_failures: int = 0
    missing_elements: int = 0
    slow_pages: int = 0
    findings: List[Finding] = field(default_factory=list)
    unique_findings: List[Finding] = field(default_factory=list)
    site_score: int = 100
    risk_level: str = "Unknown"
    phase2_summary: str = ""
    score_breakdown: dict[str, int] = field(default_factory=dict)
    score_deductions: dict[str, list[str]] = field(default_factory=dict)
    score_weights: dict[str, float] = field(default_factory=dict)
    executive_summary: str = ""

    @property
    def total_findings(self) -> int:
        return len(self.findings)
