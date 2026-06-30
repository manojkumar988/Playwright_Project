import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qa_platform.models import Evidence, Finding, TestReport
from qa_platform.reporting import format_raw_report
from qa_platform.scanner import Phase1Tester


class _FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakePlaywrightPage:
    def __init__(self, url: str):
        self.url = url
        self.handlers = {}
        self.closed = False
        class _Video:
            def path(self_nonlocal):
                return "/tmp/fake-video.webm"

        self.video = _Video()

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, wait_until="networkidle"):
        self.url = url
        if "console" in self.handlers:
            class _Msg:
                type = "error"
                text = "console error boom"

            self.handlers["console"](_Msg())
        return type("Resp", (), {"status": 200})()

    def wait_for_load_state(self, state, timeout=5000):
        return None

    def content(self):
        return "<html><body><a href='login.html'>Login</a><script>console.error('boom')</script></body></html>"

    def locator(self, selector):
        page = self

        class _Locator:
            def __init__(self):
                self.first = self
                self.page = page

            def count(self_nonlocal):
                return 1

            def inner_text(self_nonlocal, timeout=1000):
                return "Home Login"

            def evaluate_all(self_nonlocal, script):
                return ["https://demo-webshop.com/login"]

            def scroll_into_view_if_needed(self_nonlocal, timeout=None):
                return None

            def click(self_nonlocal, timeout=None):
                self_nonlocal.page.url = "https://demo-webshop.com/login"
                return None

        return _Locator()

    def evaluate(self, script):
        return [{"kind": "link", "text": "Login", "href": "https://demo-webshop.com/login"}]

    def get_by_text(self, text, exact=False):
        return self.locator(text)

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"fake screenshot")

    def close(self):
        self.closed = True
        return None


class _FakePlaywrightContext:
    def __init__(self):
        self.created_pages = []

    def new_page(self):
        page = _FakePlaywrightPage("file:///tmp/index.html")
        self.created_pages.append(page)
        return page

    def close(self):
        return None


class _FakePlaywrightBrowser:
    def new_context(self, ignore_https_errors=True, viewport=None, record_video_dir=None, record_video_size=None):
        return _FakePlaywrightContext()

    def close(self):
        return None


class _FakePlaywrightChromium:
    def launch(self, headless=True, args=None):
        return _FakePlaywrightBrowser()


class _FakePlaywright:
    chromium = _FakePlaywrightChromium()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_urlopen(request, timeout=10.0):
    url = request.full_url
    pages = {
        "https://demo-webshop.com/": "<html><body><h1>Home</h1><a href='/login'>Login</a><a href='/broken'>Broken</a></body></html>",
        "https://demo-webshop.com/home": "<html><body><h1>Home</h1></body></html>",
        "https://demo-webshop.com/login": "<html><body><h1>Sign in</h1><p>Password</p></body></html>",
        "https://demo-webshop.com/products": "<html><body><h1>Products</h1><script>console.log('ok')</script></body></html>",
        "https://demo-webshop.com/cart": "<html><body><h1>Cart</h1></body></html>",
        "https://demo-webshop.com/checkout": "<html><body><h1>Checkout</h1></body></html>",
        "https://demo-webshop.com/broken": None,
    }
    if url not in pages or pages[url] is None:
        raise HTTPError(url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))
    return _FakeResponse(pages[url])


class Phase1ScaffoldTests(unittest.TestCase):
    def test_report_counts_pages_and_broken_links(self) -> None:
        with patch("qa_platform.scanner.urlopen", side_effect=_fake_urlopen):
            report = Phase1Tester("https://demo-webshop.com", browser_mode="http").run()

        self.assertGreaterEqual(report.pages_tested, 1)
        self.assertGreaterEqual(report.broken_links, 1)
        self.assertGreaterEqual(report.total_findings, 1)

    def test_browser_mode_uses_real_browser(self) -> None:
        with patch("qa_platform.scanner.sync_playwright", return_value=_FakePlaywright()):
            report = Phase1Tester("file:///tmp/index.html", browser_mode="browser").run()

        self.assertGreaterEqual(report.pages_tested, 1)
        self.assertGreaterEqual(len(report.recordings), 1)
        self.assertGreaterEqual(report.total_findings, 1)

    def test_browser_action_restores_same_page_after_same_tab_navigation(self) -> None:
        tester = Phase1Tester("https://demo-webshop.com", browser_mode="browser")
        page = _FakePlaywrightPage("https://demo-webshop.com/")

        with patch.object(Phase1Tester, "_collect_page_findings"):
            with patch.object(Phase1Tester, "_scroll_page"):
                with patch.object(Phase1Tester, "_capture_browser_evidence"):
                    with patch.object(Phase1Tester, "_page_is_closed", return_value=False):
                        tester._exercise_visible_actions(
                            report=TestReport(target_url="https://demo-webshop.com"),
                            context=type("Ctx", (), {"pages": lambda self=None: [page]})(),
                            page=page,
                            current_url="https://demo-webshop.com/",
                            actions=[{"kind": "link", "text": "Login", "href": "https://demo-webshop.com/login"}],
                            seen=set(),
                            queue=[],
                            base_url="https://demo-webshop.com/",
                        )

        self.assertEqual(page.url, "https://demo-webshop.com/")

    def test_browser_loop_reopens_closed_page(self) -> None:
        tester = Phase1Tester("https://demo-webshop.com", browser_mode="browser")
        context = _FakePlaywrightContext()
        page = context.new_page()
        page.closed = True

        reopened = tester._open_scan_page(context, [], [])

        self.assertIsNot(page, reopened)
        self.assertEqual(len(context.created_pages), 2)

    def test_raw_report_rendering(self) -> None:
        report = TestReport(
            target_url="https://demo-webshop.com",
            pages_tested=1,
            recordings=["/tmp/video.webm"],
            findings=[
                Finding(
                    category="broken_link",
                    message="Broken link",
                    evidence=[Evidence(kind="url", value="https://demo-webshop.com/broken", note="404")],
                )
            ],
        )
        rendered = format_raw_report(report)
        self.assertIn("Target URL: https://demo-webshop.com", rendered)
        self.assertIn("Pages Tested: 1", rendered)
        self.assertIn("Recordings: 1", rendered)
        self.assertIn("Resource Failures:", rendered)
        self.assertIn("Findings:", rendered)
        self.assertIn("broken_link", rendered)
        self.assertIn("Phase 1 Result:", rendered)
        self.assertIn("[Medium]", rendered)

    def test_phase2_summary_and_crawl_labels(self) -> None:
        report = TestReport(
            target_url="https://www.think41.com/",
            pages_tested=2,
            findings=[
                Finding(category="slow_page", message="slow"),
                Finding(category="resource_failure", message="resource"),
                Finding(category="resource_failure", message="resource"),
            ],
        )
        report.unique_findings = [Finding(category="slow_page", message="slow"), Finding(category="resource_failure", message="resource")]
        report.site_score = 88
        report.risk_level = "Moderate"
        report.phase2_summary = "Site score: 88/100. Risk level: Moderate. Unique findings: 2."
        report.score_weights = {
            "Functional Quality": 0.40,
            "Performance": 0.25,
            "Navigation": 0.15,
            "Resource Health": 0.10,
            "API Health": 0.10,
        }
        report.page_summaries = []
        rendered = format_raw_report(report)
        self.assertIn("Phase 2 Summary:", rendered)
        self.assertIn("Site Score: 88/100", rendered)
        self.assertIn("Risk Level: Moderate", rendered)
        self.assertIn("Unique Findings: 2", rendered)
        self.assertIn("Total Findings (raw): 3", rendered)
        self.assertIn("Slow Pages Raw:", rendered)
        self.assertIn("Slow Pages Unique:", rendered)
        self.assertIn("Overall Score Formula:", rendered)

        from qa_platform.reporting import _friendly_page_label

        self.assertEqual(_friendly_page_label("https://www.think41.com/"), "Home")
        self.assertEqual(_friendly_page_label("https://www.think41.com/about", "Think41"), "About")
        self.assertEqual(_friendly_page_label("https://www.think41.com/offering-a", "Think41"), "Offering A")

    def test_moderate_high_risk_band(self) -> None:
        tester = Phase1Tester("https://demo-webshop.com", browser_mode="http")
        report = TestReport(target_url="https://demo-webshop.com")
        report.pages_tested = 1
        report.findings = [
            Finding(category="js_error", message="boom"),
            Finding(category="resource_failure", message="asset"),
        ]
        unique = tester._dedupe_findings(report.findings)
        report.site_score, report.risk_level = tester._score_report(report, unique)
        self.assertEqual(report.risk_level, "Moderate-High")

    def test_unique_slow_pages_drive_recommendation(self) -> None:
        tester = Phase1Tester("https://demo-webshop.com", browser_mode="http")
        findings = [
            Finding(category="slow_page", message="Page load took 15.25s", url="https://demo-webshop.com/terms"),
            Finding(category="slow_page", message="Page load took 14.10s", url="https://demo-webshop.com/terms/"),
            Finding(category="slow_page", message="Page load took 6.15s", url="https://demo-webshop.com/about"),
        ]
        report = TestReport(target_url="https://demo-webshop.com", slow_pages=3)
        report.site_score = 86
        report.navigation_failures = 1
        summary = tester._build_executive_summary(report, findings)
        breakdown, deductions = tester._score_breakdown(report, findings)
        self.assertEqual(breakdown["Performance"], 80)
        self.assertEqual(len(deductions["Performance"]), 2)
        self.assertIn("Optimize slow pages first", summary)
        self.assertIn("Priority 1", summary)
        self.assertIn("Terms", summary)
        self.assertNotIn("Navigation is healthy", summary)
        self.assertIn("Navigation issue found", summary)

    def test_crawl_graph_uses_friendly_labels(self) -> None:
        report = TestReport(
            target_url="https://www.think41.com/",
            page_summaries=[
                type("Page", (), {"url": "https://www.think41.com/", "title": "Think41", "status": 200, "response_time_seconds": 1.0, "parent_url": None, "discovered_from": None, "duplicate_of": None})(),
                type("Page", (), {"url": "https://www.think41.com/about", "title": "Think41", "status": 200, "response_time_seconds": 1.0, "parent_url": "https://www.think41.com/", "discovered_from": None, "duplicate_of": None})(),
                type("Page", (), {"url": "https://www.think41.com/offering-a", "title": "Think41", "status": 200, "response_time_seconds": 1.0, "parent_url": "https://www.think41.com/", "discovered_from": None, "duplicate_of": None})(),
                type("Page", (), {"url": "https://www.think41.com/privacy-policy", "title": "Think41", "status": 200, "response_time_seconds": 1.0, "parent_url": "https://www.think41.com/", "discovered_from": None, "duplicate_of": None})(),
            ],
        )
        rendered = format_raw_report(report)
        self.assertIn("Home", rendered)
        self.assertIn("About", rendered)
        self.assertIn("Offering A", rendered)
        self.assertIn("Privacy Policy", rendered)


if __name__ == "__main__":
    unittest.main()
