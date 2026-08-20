from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from .models import Evidence, Finding, PageSummary, TestReport
from .scanner_config import SLOW_PAGE_THRESHOLD_SECONDS


class ReportBuilderMixin:
    def _collect_page_findings(self, report: TestReport, page: _PageResult) -> None:
        if page.error:
            category = "broken_link" if page.status and page.status >= 400 else "api_failure"
            report.findings.append(
                Finding(
                    category=category,
                    message=f"Failed to load page: {page.error}",
                    url=page.url,
                    evidence=[self.evidence_for_url(page.url, note=page.error)],
                )
            )
            return

        if page.status and page.status >= 400:
            report.findings.append(
                Finding(
                    category="broken_link",
                    message=f"Page returned HTTP {page.status}",
                    url=page.url,
                    evidence=[self.evidence_for_url(page.url, note=f"HTTP {page.status}")],
                )
            )
        if page.duration_seconds >= SLOW_PAGE_THRESHOLD_SECONDS:
            report.findings.append(
                Finding(
                    category="slow_page",
                    message=f"Page load took {page.duration_seconds:.2f}s",
                    url=page.url,
                    evidence=[self.evidence_for_url(page.url, note=f"{page.duration_seconds:.2f}s")],
                )
            )
        if self._looks_like_js_error(page.html):
            report.findings.append(
                Finding(
                    category="js_error",
                    message="Potential JavaScript runtime error markers found in page source",
                    url=page.url,
                    evidence=[self.evidence_for_url(page.url, note="javascript error marker")],
                )
            )
        if self._should_check_missing_element(page.url, page.text):
            report.findings.append(
                Finding(
                    category="missing_element",
                    message="Did not find expected content on page",
                    url=page.url,
                    evidence=[self.evidence_for_url(page.url, note="expected flow content missing")],
                )
            )

    def _record_page_summary(self, report: TestReport, page: _PageResult, duplicate_of: str | None = None, parent_url: str | None = None) -> None:
        report.page_summaries.append(
            PageSummary(
                url=page.url,
                title=page.title,
                status=page.status,
                response_time_seconds=page.duration_seconds,
                parent_url=parent_url,
                duplicate_of=duplicate_of,
            )
        )

    def _collect_link_findings(self, report: TestReport, source_page: _PageResult, linked_page: _PageResult) -> None:
        if linked_page.error or (linked_page.status and linked_page.status >= 400):
            report.findings.append(
                Finding(
                    category="broken_link",
                    message=f"Broken link from {source_page.url} to {linked_page.url}",
                    url=linked_page.url,
                    evidence=[self.evidence_for_url(linked_page.url, note=linked_page.error or f"HTTP {linked_page.status}")],
                )
            )
        if self._looks_like_api_failure(linked_page.url, linked_page.html, linked_page.status):
            report.findings.append(
                Finding(
                    category="api_failure",
                    message=f"API-like request failed for {linked_page.url}",
                    url=linked_page.url,
                    evidence=[self.evidence_for_url(linked_page.url, note=linked_page.error or f"HTTP {linked_page.status}")],
                )
            )

    def _aggregate_counts(self, report: TestReport) -> None:
        unique_findings = self._dedupe_findings(report.findings)
        report.broken_links = 0
        report.js_errors = 0
        report.api_failures = 0
        report.resource_failures = 0
        report.third_party_failures = 0
        report.navigation_failures = 0
        report.missing_elements = 0
        report.slow_pages = 0
        report.unique_findings = unique_findings
        for finding in report.findings:
            if finding.category == "broken_link":
                report.broken_links += 1
            elif finding.category == "js_error":
                report.js_errors += 1
            elif finding.category == "api_failure":
                report.api_failures += 1
            elif finding.category == "resource_failure":
                report.resource_failures += 1
            elif finding.category == "third_party_failure":
                report.third_party_failures += 1
            elif finding.category == "navigation_failure":
                report.navigation_failures += 1
            elif finding.category == "missing_element":
                report.missing_elements += 1
            elif finding.category == "slow_page":
                report.slow_pages += 1
        report.score_breakdown, report.score_deductions = self._score_breakdown(report, unique_findings)
        report.site_score, report.risk_level = self._score_report(report, unique_findings)
        report.score_weights = self._score_weights()
        report.phase2_summary = self._build_phase2_summary(report, unique_findings)
        report.executive_summary = self._build_executive_summary(report, unique_findings)

    def partial_report(self) -> TestReport | None:
        """Return the report collected so far, finalized for persistence and display."""
        if self.latest_report is not None:
            self._aggregate_counts(self.latest_report)
        return self.latest_report

    def _dedupe_findings(self, findings: list[Finding]) -> list[Finding]:
        seen: set[tuple[str, str | None, str]] = set()
        unique: list[Finding] = []
        for finding in findings:
            fingerprint = (finding.category, self._strip_fragment(self._normalize_url_like(finding.url or "")), finding.message)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(finding)
        return unique

    @staticmethod
    def _normalize_url_like(url: str) -> str:
        if not url:
            return url
        parsed = urlparse(url)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    @staticmethod
    def _score_for_category(category: str) -> int:
        return {
            "navigation_failure": 25,
            "js_error": 20,
            "broken_link": 12,
            "api_failure": 10,
            "slow_page": 6,
            "missing_element": 8,
            "resource_failure": 3,
            "third_party_failure": 1,
        }.get(category, 2)

    def _score_breakdown(self, report: TestReport, findings: list[Finding]) -> tuple[dict[str, int], dict[str, list[str]]]:
        categories = {
            "Functional Quality": 100,
            "Performance": 100,
            "Resource Health": 100,
            "Navigation": 100,
            "API Health": 100,
        }
        deductions: dict[str, list[str]] = {key: [] for key in categories}
        seen_slow_pages: set[str] = set()
        for finding in findings:
            note = finding.message
            if finding.category == "slow_page":
                page_key = self._normalize_url_like(finding.url or "")
                if page_key in seen_slow_pages:
                    continue
                seen_slow_pages.add(page_key)
                categories["Performance"] -= 10
                deductions["Performance"].append(f"-10 {note}")
            elif finding.category == "resource_failure":
                categories["Resource Health"] -= 10
                deductions["Resource Health"].append(f"-10 {note}")
            elif finding.category == "third_party_failure":
                categories["Resource Health"] -= 5
                deductions["Resource Health"].append(f"-5 {note}")
            elif finding.category == "navigation_failure":
                categories["Navigation"] -= 20
                deductions["Navigation"].append(f"-20 {note}")
            elif finding.category == "broken_link":
                categories["Functional Quality"] -= 8
                deductions["Functional Quality"].append(f"-8 {note}")
            elif finding.category == "js_error":
                categories["Functional Quality"] -= 15
                deductions["Functional Quality"].append(f"-15 {note}")
            elif finding.category == "api_failure":
                categories["API Health"] -= 15
                deductions["API Health"].append(f"-15 {note}")
            elif finding.category == "missing_element":
                categories["Functional Quality"] -= 5
                deductions["Functional Quality"].append(f"-5 {note}")
        if report.pages_tested:
            for key in categories:
                categories[key] = max(0, min(100, categories[key]))
        return categories, deductions

    def _score_report(self, report: TestReport, findings: list[Finding]) -> tuple[int, str]:
        breakdown, _ = self._score_breakdown(report, findings)
        weights = self._score_weights()
        score = round(sum(breakdown[label] * weight for label, weight in weights.items()))
        zero_categories = [label for label, value in breakdown.items() if value == 0]
        categories = {finding.category for finding in findings}
        if zero_categories:
            if "Navigation" in zero_categories or "Functional Quality" in zero_categories:
                return score, "Critical"
            if "Performance" in zero_categories or "Resource Health" in zero_categories:
                return score, "Moderate-High"
        if "navigation_failure" in categories or "js_error" in categories:
            if score >= 55:
                level = "Moderate-High"
            elif score >= 35:
                level = "High"
            else:
                level = "Critical"
            return score, level
        if score >= 90:
            level = "Low"
        elif score >= 70:
            level = "Moderate"
        elif score >= 55:
            level = "Moderate-High"
        elif score >= 40:
            level = "High"
        else:
            level = "Critical"
        return score, level

    def _build_phase2_summary(self, report: TestReport, findings: list[Finding]) -> str:
        unique_slow_pages = len(self._unique_slow_pages(findings))
        return (
            f"Site score: {report.site_score}/100 using weighted category scores. "
            f"Risk level: {report.risk_level}. "
            f"Unique findings: {len(findings)}. "
            f"Deduplicated from {report.total_findings} total findings. "
            f"Slow pages raw: {report.slow_pages}. "
            f"Slow pages unique: {unique_slow_pages}."
        )

    def _build_executive_summary(self, report: TestReport, findings: list[Finding]) -> str:
        slow_pages = self._unique_slow_pages(findings)
        strengths: list[str] = []
        weaknesses: list[str] = []
        if report.broken_links == 0:
            strengths.append("No broken links")
        if report.navigation_failures == 0:
            strengths.append("Navigation is healthy")
        if report.api_failures == 0:
            strengths.append("APIs are healthy")
        if report.navigation_failures:
            weaknesses.append("Navigation issue found")
        if slow_pages:
            weaknesses.append(f"{len(slow_pages)} unique slow pages")
        if report.resource_failures:
            weaknesses.append(f"{report.resource_failures} resource issues")
        if report.js_errors:
            weaknesses.append(f"{report.js_errors} JavaScript errors")
        recommendation_parts = []
        if slow_pages:
            top_slow_pages = ", ".join(
                f"Priority {index + 1}: {page_label} ({duration:.2f}s)"
                for index, (page_label, duration) in enumerate(slow_pages[:3])
            )
            recommendation_parts.append(f"Optimize slow pages first: {top_slow_pages}")
        if report.resource_failures:
            recommendation_parts.append("Fix missing resources and banners")
        if report.js_errors:
            recommendation_parts.append("Resolve runtime errors")
        if not recommendation_parts:
            recommendation_parts.append("Continue monitoring the site")
        lines = [
            "Website Health Summary",
            f"Overall Score: {report.site_score}/100",
            f"Risk: {report.risk_level}",
        ]
        if strengths:
            lines.append("Strengths: " + "; ".join(strengths))
        if weaknesses:
            lines.append("Weaknesses: " + "; ".join(weaknesses))
        lines.append("Recommendation: " + "; ".join(recommendation_parts))
        return "\n".join(lines)

    @staticmethod
    def _score_weights() -> dict[str, float]:
        return {
            "Functional Quality": 0.40,
            "Performance": 0.25,
            "Navigation": 0.15,
            "Resource Health": 0.10,
            "API Health": 0.10,
        }

    def _unique_slow_pages(self, findings: list[Finding]) -> list[tuple[str, float]]:
        unique: dict[str, tuple[str, float]] = {}
        for finding in findings:
            if finding.category != "slow_page" or not finding.url:
                continue
            canonical = self._normalize_url_like(finding.url)
            duration = self._extract_duration(finding.message)
            current = unique.get(canonical)
            if current is None or duration > current[1]:
                unique[canonical] = (self._friendly_page_label_from_url(finding.url), duration)
        return sorted(unique.values(), key=lambda item: item[1], reverse=True)

    @staticmethod
    def _extract_duration(message: str) -> float:
        import re

        match = re.search(r"(\d+(?:\.\d+)?)s", message)
        return float(match.group(1)) if match else 0.0

    @staticmethod
    def _friendly_page_label_from_url(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return "Home"
        tail = path.rsplit("/", 1)[-1]
        words = [part for part in tail.replace("-", " ").replace("_", " ").split() if part]
        if not words:
            return "Page"
        return " ".join(word.capitalize() for word in words)

