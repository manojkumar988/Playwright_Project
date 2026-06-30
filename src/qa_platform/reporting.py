from __future__ import annotations

from urllib.parse import urlparse

from .models import TestReport


def format_raw_report(report: TestReport) -> str:
    lines = [
        f"Target URL: {report.target_url}",
        f"Pages Tested: {report.pages_tested}",
    ]
    if report.tested_urls:
        lines.append("Tested Pages:")
        for url in report.tested_urls:
            lines.append(f"- {url}")
    if report.clicked_urls:
        lines.append("Clicked Links:")
        for url in report.clicked_urls:
            lines.append(f"- {url}")
    if report.page_summaries:
        lines.append("Page Summaries:")
        for page in report.page_summaries:
            parts = [page.url]
            if page.title:
                parts.append(f"title={page.title}")
            if page.status is not None:
                parts.append(f"status={page.status}")
            parts.append(f"time={page.response_time_seconds:.2f}s")
            if page.duplicate_of:
                parts.append(f"duplicate_of={page.duplicate_of}")
            if page.discovered_from:
                parts.append(f"discovered_from={page.discovered_from}")
            if page.parent_url:
                parts.append(f"parent={page.parent_url}")
            lines.append(f"- {' | '.join(parts)}")
        tree = _build_crawl_graph(report)
        if tree:
            lines.append("Crawl Graph:")
            lines.extend(tree)
    if report.phase2_summary:
        lines.append("Phase 2 Summary:")
        lines.append(f"- {report.phase2_summary}")
    if report.score_breakdown:
        lines.append("Score Breakdown:")
        for label, value in report.score_breakdown.items():
            lines.append(f"- {label}: {value}/100")
            deductions = report.score_deductions.get(label, [])
            for deduction in deductions:
                lines.append(f"  {deduction}")
    if report.score_weights:
        lines.append("Overall Score Formula:")
        for label, weight in report.score_weights.items():
            lines.append(f"- {label}: {weight * 100:.0f}%")
    if report.executive_summary:
        lines.append("Executive Summary:")
        lines.extend(f"- {line}" for line in report.executive_summary.splitlines())
    lines.extend(
        [
            f"Site Score: {report.site_score}/100",
            f"Risk Level: {report.risk_level}",
            f"Total Findings (raw): {report.total_findings}",
            f"Unique Findings: {len(report.unique_findings)}",
            f"Recordings: {len(report.recordings)}",
            f"Broken Links: {report.broken_links}",
            f"JS Errors: {report.js_errors}",
            f"API Failures: {report.api_failures}",
            f"Resource Failures: {report.resource_failures}",
            f"Third Party Failures: {report.third_party_failures}",
            f"Navigation Failures: {report.navigation_failures}",
            f"Missing Elements: {report.missing_elements}",
            f"Slow Pages Raw: {report.slow_pages}",
            f"Slow Pages Unique: {len(_unique_slow_pages(report))}",
            f"Total Findings: {report.total_findings}",
        ]
    )
    if report.findings:
        lines.append("Findings:")
        for finding in report.findings:
            severity = _severity_for_finding(finding.category)
            evidence_parts = []
            for item in finding.evidence:
                parts = [f"{item.kind}:{item.value}"]
                if item.note:
                    parts.append(f"note={item.note}")
                if item.screenshot_path:
                    parts.append(f"screenshot={item.screenshot_path}")
                if item.recording_path:
                    parts.append(f"recording={item.recording_path}")
                evidence_parts.append(", ".join(parts))
            evidence = "; ".join(evidence_parts)
            lines.append(
                f"- [{severity}] {finding.category}: {finding.message}"
                + (f" | {evidence}" if evidence else "")
            )
    lines.append(f"Phase 1 Result: {_phase1_result(report)}")
    return "\n".join(lines)


def _build_crawl_graph(report: TestReport) -> list[str]:
    if not report.page_summaries:
        return []
    root = report.page_summaries[0].url
    children: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for page in report.page_summaries:
        labels[page.url] = _friendly_page_label(page.url, page.title)
        parent = page.parent_url or page.discovered_from or (root if page.url != root else "")
        if not parent:
            continue
        children.setdefault(parent, []).append(page.url)

    lines = [labels.get(root, root)]

    def walk(url: str, prefix: str = "") -> None:
        for index, child in enumerate(children.get(url, [])):
            last = index == len(children[url]) - 1
            branch = "└── " if last else "├── "
            lines.append(f"{prefix}{branch}{labels.get(child, child)}")
            walk(child, prefix + ("    " if last else "│   "))

    walk(root)
    return lines


def _friendly_page_label(url: str, title: str = "") -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return "Home"
    slug_map = {
        "about": "About",
        "about-us": "About",
        "offering-a": "Offering A",
        "offering-b": "Offering B",
        "careers": "Careers",
        "contact": "Contact",
        "privacy-policy": "Privacy Policy",
    }
    if path in slug_map:
        return slug_map[path]
    if title.strip():
        return title.strip()
    tail = path.rsplit("/", 1)[-1]
    words = [part for part in tail.replace("-", " ").replace("_", " ").split() if part]
    if not words:
        return title.strip() or "Home"
    return " ".join(word.capitalize() for word in words)


def _severity_for_finding(category: str) -> str:
    mapping = {
        "navigation_failure": "High",
        "broken_link": "Medium",
        "api_failure": "Medium",
        "resource_failure": "Low",
        "third_party_failure": "Low",
        "slow_page": "Medium",
        "js_error": "High",
        "missing_element": "Medium",
    }
    return mapping.get(category, "Info")


def _phase1_result(report: TestReport) -> str:
    if report.total_findings == 0:
        return "Passed"
    if report.navigation_failures == 0 and report.broken_links == 0 and report.js_errors == 0 and report.api_failures == 0:
        return "Passed with warnings"
    return "Passed with warnings"


def _unique_slow_pages(report: TestReport) -> list[tuple[str, float]]:
    seen: dict[str, tuple[str, float]] = {}
    for finding in report.findings:
        if finding.category != "slow_page" or not finding.url:
            continue
        parsed = urlparse(finding.url)
        path = parsed.path.rstrip("/") or "/"
        canonical = f"{parsed.scheme}://{parsed.netloc}{path}"
        match = None
        for token in finding.message.split():
            if token.endswith("s"):
                try:
                    match = float(token.rstrip("s"))
                    break
                except ValueError:
                    continue
        if match is None:
            match = 0.0
        label = _friendly_page_label(finding.url)
        current = seen.get(canonical)
        if current is None or match > current[1]:
            seen[canonical] = (label, match)
    return sorted(seen.values(), key=lambda item: item[1], reverse=True)
