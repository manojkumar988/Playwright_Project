from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from tempfile import NamedTemporaryFile
import threading
import ipaddress
import socket
from pathlib import Path
from time import perf_counter
from typing import Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .models import Evidence, Finding, PageSummary, TestReport
from .action_planner import ActionPlannerMixin
from .report_builder import ReportBuilderMixin
from .scanner_config import (
    CONTENT_LINK_TERMS, HIGH_PRIORITY_LINK_TERMS, HIGH_VALUE_PATH_PARTS,
    LEGAL_FOOTER_LINK_TERMS, LOW_VALUE_LINK_PATH_PARTS, SPECIAL_SERVICE_LINK_TERMS,
    SUPPORT_LINK_TERMS, UTILITY_LINK_TERMS,
)

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
except Exception:  # pragma: no cover - optional dependency
    Browser = BrowserContext = Page = object  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


MAX_PAGES = 30
BROWSER_MAX_PAGES = 15
MAX_ACTIONS_PER_PAGE = 6
MAX_NEW_LINKS_PER_PAGE = 8
MAX_LINKS_PER_TOP_SECTION = 2
MAX_DEEP_LINKS_PER_TOP_SECTION = 1
MIN_QUEUED_LINKS_TO_SKIP_ACTIONS = 999
SLOW_PAGE_THRESHOLD_SECONDS = 3.0
POST_NAVIGATION_PAUSE_MS = 150
POPUP_DISMISS_TRIES = 2
ACTION_TIMEOUT_MS = 1500
POPUP_WAIT_TIMEOUT_MS = 700
POPUP_DISMISS_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("Accept all")',
    'button:has-text("Allow all")',
    'button:has-text("Agree")',
    'button:has-text("I agree")',
    'button:has-text("OK")',
    'button:has-text("Got it")',
    'button:has-text("Close")',
    'button:has-text("Dismiss")',
    'button:has-text("No thanks")',
    'button:has-text("Not now")',
    'button:has-text("Maybe later")',
    'button:has-text("Skip")',
    'button:has-text("Reject")',
    'button[aria-label*="close" i]',
    'button[aria-label*="dismiss" i]',
    'button[title*="close" i]',
    'button[title*="dismiss" i]',
    '[role="button"][aria-label*="close" i]',
    '[role="button"][aria-label*="dismiss" i]',
    '[data-testid*="close" i]',
    '[data-testid*="dismiss" i]',
    '[id*="onetrust-close" i]',
    '[id*="onetrust-reject" i]',
    '[id*="onetrust-accept" i]',
    '[role="dialog"] button[aria-label*="close" i]',
    '[aria-modal="true"] button[aria-label*="close" i]',
    'dialog button[aria-label*="close" i]',
    '[class*="modal" i] button[aria-label*="close" i]',
    '[class*="modal" i] button:has-text("Close")',
    '[role="dialog"] button:has-text("Not now")',
    '[role="dialog"] button:has-text("No thanks")',
]
IGNORED_NETWORK_HOSTS = {
    "www.google-analytics.com",
    "google-analytics.com",
    "www.googletagmanager.com",
    "googletagmanager.com",
}
IGNORED_FAILURE_MARKERS = (
    "net::err_aborted",
    "net::err_blocked_by_client",
    "net::err_failed",
)

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "source",
    "spm",
    "ved",
    "ei",
    "sig",
    "sxsrf",
}
LOW_VALUE_PATH_PARTS = {
    "setprefs",
    "preferences",
    "settings",
    "locale",
    "language",
    "languages",
    "translate",
    "history",
    "privacyadvisor",
    "logout",
    "signout",
    "unsubscribe",
}
AUTH_ACTION_TEXT = {
    "log in",
    "login",
    "sign in",
    "signin",
    "sign-in",
    "sign up",
    "signup",
    "register",
    "my account",
    "account",
}
AUTH_PATH_PARTS = {
    "account",
    "accounts",
    "auth",
    "oauth",
    "login",
    "signin",
    "sign-in",
    "signup",
    "register",
    "sso",
    "session",
}
AUTH_HOST_PARTS = {
    "account",
    "accounts",
    "auth",
    "login",
    "signin",
    "oauth",
    "sso",
    "identity",
}
LOW_VALUE_QUERY_KEYS = {"hl", "lang", "locale", "theme"}
IGNORED_CRAWL_EXTENSIONS = {
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".map",
    ".json",
    ".xml",
    ".txt",
    ".mp4",
    ".webm",
}


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.text_chunks: list[str] = []
        self.title_chunks: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs):
        attrs = dict(attrs)
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        if tag in {"img", "script"}:
            src = attrs.get("src")
            if src:
                self.links.append(src)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_chunks.append(data.strip())
            if self._in_title:
                self.title_chunks.append(data.strip())


@dataclass
class _PageResult:
    url: str
    status: Optional[int]
    duration_seconds: float
    html: str
    links: list[str]
    text: str
    title: str = ""
    error: Optional[str] = None


@dataclass
class Phase1Tester(ReportBuilderMixin, ActionPlannerMixin):
    target_url: str
    timeout_seconds: float = 30.0
    browser_mode: str = "auto"
    headless: bool = False
    fast_browser: bool = False
    logger: callable = field(default=lambda message: print(message, flush=True), repr=False, compare=False)
    stop_event: threading.Event | None = field(default=None, repr=False, compare=False)
    runtime_hook: callable | None = field(default=None, repr=False, compare=False)
    latest_report: TestReport | None = field(default=None, init=False, repr=False, compare=False)

    def _should_stop(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _raise_if_stopped(self) -> None:
        if self._should_stop():
            raise RuntimeError("Scan stopped")

    def _set_runtime(self, *, browser=None, context=None, page=None) -> None:
        if self.runtime_hook is not None:
            self.runtime_hook(browser=browser, context=context, page=page)

    @staticmethod
    def _has_blocking_popup(page: Page) -> bool:
        selectors = [
            '[role="dialog"]', '[aria-modal="true"]', 'dialog[open]',
            '[class*="modal" i]', '[class*="overlay" i]', '[class*="popup" i]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 5)):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _dismiss_interruptions(self, page: Page) -> bool:
        dismissed = False
        self._raise_if_stopped()
        try:
            page.mouse.click(8, 8)
        except Exception:
            self._raise_if_stopped()

        scopes: list[tuple[str, object]] = [("page", page)]
        try:
            frames = getattr(page, "frames", [])
            for index, frame in enumerate(frames[1:], start=1):
                scopes.append((f"frame-{index}", frame))
        except Exception:
            self._raise_if_stopped()

        for _ in range(POPUP_DISMISS_TRIES):
            self._raise_if_stopped()
            if self._page_is_closed(page):
                return dismissed
            dismissed_this_round = False
            for scope_name, scope in scopes:
                for selector in POPUP_DISMISS_SELECTORS:
                    self._raise_if_stopped()
                    try:
                        locator = scope.locator(selector).first
                        if locator.count() == 0 or not locator.is_visible():
                            continue
                        locator.click(timeout=ACTION_TIMEOUT_MS, force=True)
                        dismissed = True
                        dismissed_this_round = True
                        self._log(f"[browser] Dismissed interruption ({scope_name}): {selector}")
                        try:
                            page.wait_for_timeout(200)
                        except Exception:
                            self._raise_if_stopped()
                        break
                    except Exception:
                        self._raise_if_stopped()
                        continue
                if dismissed_this_round:
                    break
            if not dismissed_this_round:
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                    if not self._has_blocking_popup(page):
                        dismissed = True
                        self._log("[browser] Dismissed interruption with Escape")
                        break
                except Exception:
                    self._raise_if_stopped()
                break
        if self._has_blocking_popup(page):
            self._log("[browser] Blocking popup remains after close attempts")
        return dismissed

    def run(self) -> TestReport:
        report = TestReport(target_url=self.target_url)
        self.latest_report = report

        self._raise_if_stopped()
        base_url = self._normalize_url(self.target_url)
        self._log(f"Starting crawl: {base_url}")
        if self._should_use_browser():
            self._log(f"Mode: browser (headless={self.headless}, fast={self.fast_browser})")
            self._run_browser(report, base_url)
            self._aggregate_counts(report)
            return report

        self._log("Mode: http")
        seen: Set[str] = set()
        seen_roots: dict[str, str] = {}
        queue: list[tuple[str, str | None]] = [(base_url, None)]
        while queue and len(seen) < MAX_PAGES:
            self._raise_if_stopped()
            url, parent_url = queue.pop(0)
            normalized = self._strip_fragment(url)
            if normalized in seen:
                continue
            duplicate_of = seen_roots.get(self._duplicate_key(normalized))
            seen.add(normalized)
            if self._duplicate_key(normalized) not in seen_roots:
                seen_roots[self._duplicate_key(normalized)] = normalized

            self._log(f"[{len(seen)}/{MAX_PAGES}] Fetching page: {normalized}")
            page = self._fetch_page(normalized)
            self._collect_page_findings(report, page)
            self._record_page_summary(report, page, duplicate_of=duplicate_of, parent_url=parent_url)
            report.pages_tested += 1
            report.tested_urls.append(normalized)
            if page.html:
                inserted = self._insert_next_links(queue, self._discover_links(base_url, page, seen), normalized, seen)
                if inserted:
                    self._log(f"Discovered {len(inserted)} new links on current page; queued next: {self._format_queue([(link, normalized) for link in inserted])}")
                self._log(f"Queue now ({len(queue)}): {self._format_queue(queue)}")

        self._aggregate_counts(report)
        self._log("Crawl complete")
        return report

    def _should_use_browser(self) -> bool:
        if self.browser_mode == "browser":
            return self._playwright_available()
        if self.browser_mode == "http":
            return False
        return self._playwright_available()

    @staticmethod
    def _playwright_available() -> bool:
        return sync_playwright is not None

    def _run_browser(self, report: TestReport, base_url: str) -> None:
        assert sync_playwright is not None
        with sync_playwright() as playwright:
            self._raise_if_stopped()
            browser = playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--ignore-certificate-errors",
                    "--start-maximized",
                ],
            )
            try:
                self._set_runtime(browser=browser)
                self._browser_explore(report, browser, base_url)
            finally:
                self._set_runtime(browser=None, context=None, page=None)
                browser.close()

    def _browser_explore(self, report: TestReport, browser: Browser, base_url: str) -> None:
        seen: Set[str] = set()
        seen_roots: dict[str, str] = {}
        clicked_seen: Set[str] = set()
        clicked_action_keys: Set[str] = set()
        tested_form_keys: Set[str] = set()
        queue: list[tuple[str, str | None]] = [(base_url, None)]
        context = self._open_browser_context(browser)
        self._set_runtime(browser=browser, context=context, page=None)
        console_errors: list[str] = []
        network_errors: list[str] = []
        page = self._open_scan_page(context, console_errors, network_errors)
        self._set_runtime(browser=browser, context=context, page=page)
        try:
            while queue and len(seen) < BROWSER_MAX_PAGES:
                self._raise_if_stopped()
                if self._page_is_closed(page):
                    self._log("Browser page closed unexpectedly, recovering")
                    page = self._recover_scan_page(browser, context, console_errors, network_errors)
                    context = page["context"]
                    page = page["page"]
                    self._set_runtime(browser=browser, context=context, page=page)
                normalized, parent_url = queue.pop(0)
                normalized = self._strip_fragment(normalized)
                if normalized in seen:
                    continue
                duplicate_key = self._duplicate_key(normalized)
                duplicate_url = seen_roots.get(duplicate_key)
                seen.add(normalized)
                if duplicate_key not in seen_roots:
                    seen_roots[duplicate_key] = normalized

                console_errors.clear()
                network_errors.clear()
                try:
                    self._log(f"[browser] Visiting page {len(seen)}/{BROWSER_MAX_PAGES}: {normalized}")
                    started = perf_counter()
                    try:
                        response = page.goto(
                            normalized,
                            wait_until="domcontentloaded",
                            timeout=self.timeout_seconds * 1000,
                        )
                    except TypeError:
                        response = page.goto(normalized, wait_until="domcontentloaded")
                    try:
                        page.wait_for_timeout(POST_NAVIGATION_PAUSE_MS)
                    except Exception:
                        pass
                    if self._is_http_url(page.url) and not self._is_public_http_url(page.url):
                        raise RuntimeError("Blocked redirect to a private or internal network target")
                    self._dismiss_interruptions(page)
                    popup_blocking = self._has_blocking_popup(page)
                    if popup_blocking:
                        self._log("[browser] Popup still blocks page actions; skipping clicks on this page")
                    if not self.fast_browser:
                        self._log(f"[browser] Scrolling page: {normalized}")
                        self._scroll_page(page)
                    self._raise_if_stopped()
                    duration = perf_counter() - started
                    status = response.status if response else None
                    html = page.content()
                    text = page.locator("body").inner_text(timeout=1000) if page.locator("body").count() else ""
                    links = self._browser_links(page, normalized, base_url)
                    nav_links = self._browser_nav_links(page, normalized, base_url)
                    dom_links = self._browser_dom_links(page, normalized, base_url)
                    self._log(
                        f"[browser] Page ready: status={status}, links={len(links)}, nav_links={len(nav_links)}, dom_links={len(dom_links)}, duration={duration:.2f}s"
                    )
                    title = ""
                    try:
                        title = page.title()
                    except Exception:
                        title = ""
                    page_result = _PageResult(url=normalized, status=status, duration_seconds=duration, html=html, links=links, text=text, title=title)
                    self._collect_page_findings(report, page_result)
                    self._record_page_summary(report, page_result, duplicate_of=duplicate_url, parent_url=parent_url)
                    report.pages_tested += 1
                    report.tested_urls.append(normalized)
                    for error in console_errors + network_errors:
                        category = self._classify_browser_error(error)
                        report.findings.append(
                            Finding(
                                category=category,
                                message=error,
                                url=normalized,
                                evidence=[self._capture_browser_evidence(page, normalized, note=error)],
                            )
                        )
                    if not self.fast_browser:
                        self._exercise_forms(report, page, normalized, queue, seen, base_url, clicked_seen, tested_form_keys)
                    action_links = [] if self.fast_browser or popup_blocking else self._browser_action_links(page, base_url, normalized, clicked_seen, clicked_action_keys)
                    if action_links:
                        self._log(f"[browser] Candidate actions ({len(action_links)}): {self._format_actions(action_links)}")
                    else:
                        self._log("[browser] Candidate actions (0): (none)")
                    action_hrefs = [str(action.get("href") or "") for action in action_links if action.get("href")]
                    inserted_links = self._insert_next_links(
                        queue,
                        [link for link in action_hrefs + dom_links + nav_links + links if self._is_candidate_page_url(link, base_url)],
                        normalized,
                        seen,
                    )
                    if dom_links or nav_links or links or action_hrefs:
                        if inserted_links:
                            self._log(f"[browser] Added {len(inserted_links)} priority links to run next: {self._format_queue([(link, normalized) for link in inserted_links])}")
                        else:
                            self._log("[browser] No new current-page links to add")
                        self._log(f"[browser] Queue now ({len(queue)}): {self._format_queue(queue)}")
                    if response and response.status >= 400:
                        self._capture_error_screenshot(page, normalized, f"http-{response.status}")
                except Exception as exc:
                    if self._should_stop() or str(exc) == "Scan stopped":
                        raise RuntimeError("Scan stopped")
                    self._log(f"[browser] Error on {normalized}: {exc}")
                    if self._page_is_closed(page):
                        recovered = self._recover_scan_page(browser, context, console_errors, network_errors)
                        context = recovered["context"]
                        page = recovered["page"]
                        queue.insert(0, (normalized, parent_url))
                        continue
                    if "Target page, context or browser has been closed" not in str(exc):
                        self._capture_error_screenshot(page, normalized, str(exc))
                    report.findings.append(
                        Finding(
                            category="navigation_failure",
                            message=f"Browser navigation failed: {exc}",
                            url=normalized,
                            evidence=[self.evidence_for_url(normalized, note=str(exc))],
                        )
                    )
                    continue
                finally:
                    video_path = self._capture_recording_path(page)
                    if video_path and video_path not in report.recordings:
                        report.recordings.append(video_path)
                        self._log(f"[browser] Saved testing video: {video_path}")

                if action_links:
                    self._exercise_visible_actions(report, context, page, normalized, action_links, seen, queue, base_url, clicked_seen, clicked_action_keys)
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    def _open_browser_context(self, browser: Browser) -> BrowserContext:
        # Keep recordings in an ignored repo-local folder for easy attachment.
        video_dir = Path(__file__).resolve().parents[2] / "qa-artifacts" / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "ignore_https_errors": True,
            "record_video_dir": video_dir,
            "record_video_size": {"width": 1280, "height": 720},
        }
        if self.headless:
            options["viewport"] = {"width": 1280, "height": 720}
        else:
            options["no_viewport"] = True
        self._log(f"[browser] Recording video to: {video_dir}")
        return browser.new_context(**options)

    def _recover_scan_page(
        self,
        browser: Browser,
        context: BrowserContext,
        console_errors: list[str],
        network_errors: list[str],
    ) -> dict[str, BrowserContext | Page]:
        try:
            new_page = self._open_scan_page(context, console_errors, network_errors)
            if not self._page_is_closed(new_page):
                self._log("Recovered browser page in existing context")
                self._set_runtime(browser=browser, context=context, page=new_page)
                return {"context": context, "page": new_page}
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        new_context = self._open_browser_context(browser)
        self._log("Created fresh browser context after recovery")
        new_page = self._open_scan_page(new_context, console_errors, network_errors)
        self._set_runtime(browser=browser, context=new_context, page=new_page)
        return {"context": new_context, "page": new_page}

    def _open_scan_page(self, context: BrowserContext, console_errors: list[str], network_errors: list[str]) -> Page:
        page = context.new_page()
        page.set_default_timeout(self.timeout_seconds * 1000)
        if hasattr(page, "set_default_navigation_timeout"):
            page.set_default_navigation_timeout(self.timeout_seconds * 1000)
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.on(
            "requestfailed",
            lambda request: self._record_request_failure(network_errors, request),
        )
        return page

    def _browser_links(self, page: Page, current_url: str, base_url: str) -> list[str]:
        links: list[str] = []
        for href in page.locator("a[href]").evaluate_all(
            """
            (els) => els
              .map((el) => el.href)
            """
        ):
            if not isinstance(href, str) or not self._is_http_url(href):
                continue
            if urlparse(href).netloc != urlparse(base_url).netloc:
                continue
            normalized = self._strip_fragment(href)
            if normalized != current_url:
                links.append(normalized)
        return links

    def _browser_nav_links(self, page: Page, current_url: str, base_url: str) -> list[str]:
        try:
            candidates = page.evaluate(
                """
                (baseUrl) => {
                  const selectors = [
                    'header a[href]',
                    'nav a[href]',
                    '[role="navigation"] a[href]',
                    'footer a[href]'
                  ];
                  const out = [];
                  for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                      try {
                        const href = new URL(el.href, window.location.href).href;
                        if (new URL(href).origin === new URL(baseUrl).origin) {
                          out.push(href);
                        }
                      } catch {}
                    }
                  }
                  return out;
                }
                """,
                base_url,
            )
        except Exception:
            candidates = []
        links: list[str] = []
        for href in candidates:
            if not isinstance(href, str) or not self._is_http_url(href):
                continue
            normalized = self._strip_fragment(href)
            if normalized != current_url and normalized not in links:
                links.append(normalized)
        return links

    def _browser_dom_links(self, page: Page, current_url: str, base_url: str) -> list[str]:
        try:
            candidates = page.evaluate(
                """
                (baseUrl) => {
                  const out = [];
                  const selectors = [
                    'a[href]',
                    '[role="link"][href]',
                    'button[data-href]',
                    '[data-href]',
                    'form[action]'
                  ];
                  for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                      try {
                        const raw = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('action');
                        if (!raw) continue;
                        const href = new URL(raw, window.location.href).href;
                        if (new URL(href).origin === new URL(baseUrl).origin) {
                          out.push(href);
                        }
                      } catch {}
                    }
                  }
                  return out;
                }
                """,
                base_url,
            )
        except Exception:
            candidates = []
        links: list[str] = []
        for href in candidates:
            if not isinstance(href, str) or not self._is_http_url(href):
                continue
            normalized = self._strip_fragment(href)
            if normalized != current_url and normalized not in links:
                links.append(normalized)
        return links

    def _exercise_forms(self, report: TestReport, page: Page, current_url: str, queue, seen: Set[str], base_url: str, clicked_seen: Set[str], tested_form_keys: Set[str]) -> None:
        try:
            forms = page.evaluate(
                """
                () => {
                  const visible = (el) => !!(el && el.getClientRects().length > 0);
                  const inputs = [...document.querySelectorAll('input:not([type]), input[type="search"], input[type="text"]')];
                  const useful = [];
                  for (const input of inputs) {
                    if (!visible(input) || input.disabled || input.readOnly) continue;
                    const name = `${input.name || ''} ${input.id || ''} ${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`.toLowerCase();
                    const form = input.closest('form');
                    const rect = input.getBoundingClientRect();
                    const isSearch = input.type === 'search' || /search|query|keyword|find|q\b|k\b/.test(name);
                    if (!isSearch && rect.top > 220) continue;
                    useful.push({
                      name,
                      placeholder: input.placeholder || input.getAttribute('aria-label') || input.name || 'input',
                      top: Math.max(0, Math.round(rect.top + window.scrollY)),
                      selectorHint: input.id ? `#${CSS.escape(input.id)}` : input.name ? `input[name="${CSS.escape(input.name)}"]` : '',
                      hasForm: !!form,
                      formKey: isSearch ? 'global-search' : `${input.name || input.id || input.placeholder || 'input'}:${Math.round(rect.top)}`,
                    });
                  }
                  return useful.slice(0, 2);
                }
                """
            )
        except Exception:
            forms = []
        if not forms:
            return
        for form in forms[:1]:
            self._raise_if_stopped()
            form_key = str(form.get("formKey") or "form") if isinstance(form, dict) else "form"
            if form_key in tested_form_keys:
                continue
            selector = str(form.get("selectorHint") or "") if isinstance(form, dict) else ""
            placeholder = str(form.get("placeholder") or "search") if isinstance(form, dict) else "search"
            try:
                locator = page.locator(selector).first if selector else page.locator('input[type="search"], input[name="q"], input[name="k"], input[placeholder*="search" i], input[aria-label*="search" i]').first
                if locator.count() == 0 or not locator.is_visible():
                    continue
                self._log(f"[browser] Testing form input: {placeholder}")
                tested_form_keys.add(form_key)
                before_url = self._strip_fragment(page.url)
                locator.fill("test", timeout=ACTION_TIMEOUT_MS)
                locator.press("Enter", timeout=ACTION_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=min(self.timeout_seconds * 1000, 2500))
                except Exception:
                    pass
                after_url = self._strip_fragment(page.url)
                if after_url != before_url and self._is_http_url(after_url) and urlparse(after_url).netloc == urlparse(base_url).netloc:
                    self._log(f"[browser] Form resolved to: {self._link_label(after_url)}")
                    if after_url not in clicked_seen:
                        clicked_seen.add(after_url)
                        report.clicked_urls.append(after_url)
                    if self._is_candidate_page_url(after_url, base_url):
                        self._insert_next_links(queue, [after_url], current_url, seen)
                    self._restore_page_url(page, current_url, quiet=True)
                else:
                    self._restore_page_url(page, current_url, quiet=True)
            except Exception as exc:
                if self._should_stop():
                    raise RuntimeError("Scan stopped")
                report.findings.append(
                    Finding(
                        category="navigation_failure",
                        message=f"Form input test failed: {exc}",
                        url=current_url,
                        evidence=[self.evidence_for_url(current_url, note=str(exc))],
                    )
                )
                try:
                    self._restore_page_url(page, current_url, quiet=True)
                except Exception:
                    pass

    def _exercise_visible_actions(self, report: TestReport, context: BrowserContext, page: Page, current_url: str, actions, seen: Set[str], queue, base_url: str, clicked_seen: Set[str] | None = None, clicked_action_keys: Set[str] | None = None) -> None:
        if clicked_seen is None:
            clicked_seen = set()
        if clicked_action_keys is None:
            clicked_action_keys = set()
        clicked = 0
        skipped_duplicate = 0
        skipped_low_value = 0
        skipped_unresolved = 0
        click_failures = 0
        seen_keys = {self._duplicate_key(url) for url in seen}

        for action in actions:
            self._raise_if_stopped()
            if clicked >= MAX_ACTIONS_PER_PAGE:
                break
            href = action.get("href")
            text = str(action.get("text") or "").strip()
            if not text or len(text) > 80 or self._is_low_value_action_text(text):
                skipped_low_value += 1
                continue
            action_key = self._action_key(action)
            if action_key in clicked_action_keys:
                skipped_duplicate += 1
                continue
            href_key = self._duplicate_key(href) if isinstance(href, str) and self._is_http_url(href) else ""
            if href_key and href_key in seen_keys:
                skipped_duplicate += 1
                continue
            critical_action = self._is_critical_action(action)
            if not critical_action and self._is_risky_action_link(href if isinstance(href, str) else None, text, base_url):
                skipped_low_value += 1
                continue
            if isinstance(href, str) and href and not critical_action and not self._is_candidate_page_url(href, base_url):
                skipped_low_value += 1
                continue
            try:
                label = self._link_label(href or "", text)
                self._log(f"[browser] Clicking {self._action_area_label(action)} action: {label}")
                locator = self._resolve_action_locator(page, action, href, text)
                if locator is None:
                    skipped_unresolved += 1
                    continue
                clicked += 1
                clicked_action_keys.add(action_key)
                self._dismiss_interruptions(page)
                self._highlight_action(locator, label)
                self._log(f"[browser] Highlighting {self._action_area_label(action)} action: {label}")
                try:
                    page.wait_for_timeout(350)
                except Exception:
                    pass
                before_url = page.url
                before_pages = self._context_pages(context)
                popup_page = self._click_and_capture_popup(page, locator)
                self._clear_action_highlight(page)
                opened_pages = self._context_pages(context)
                new_pages = [candidate for candidate in opened_pages if candidate not in before_pages]
                target_page = page
                if popup_page is not None:
                    target_page = popup_page
                if new_pages:
                    target_page = new_pages[-1]
                    try:
                        target_page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=min(self.timeout_seconds * 1000, 800 if self.fast_browser else 2000),
                        )
                    except Exception:
                        pass
                if self._page_is_closed(target_page):
                    click_failures += 1
                    self._focus_scan_page(page)
                    return
                if not self.fast_browser:
                    self._scroll_page(target_page)
                after_url = self._strip_fragment(target_page.url)
                if after_url != before_url and self._is_http_url(after_url) and urlparse(after_url).netloc == urlparse(base_url).netloc:
                    self._log(f"[browser] Click resolved to: {self._link_label(after_url)}")
                    queued_keys = {self._duplicate_key(item[0] if isinstance(item, tuple) else item) for item in queue}
                    inserted = []
                    if self._is_candidate_page_url(after_url, base_url) and self._duplicate_key(after_url) not in seen_keys and self._duplicate_key(after_url) not in queued_keys:
                        inserted = self._insert_next_links(queue, [after_url], current_url, seen)
                    if after_url not in clicked_seen:
                        clicked_seen.add(after_url)
                        report.clicked_urls.append(after_url)
                    if inserted:
                        self._log(f"[browser] Added clicked destination to run next: {self._link_label(after_url)}")
                    self._log(f"[browser] Queue updated ({len(queue)}): {self._format_queue(queue)}")
                    if target_page is not page:
                        self._close_extra_page(target_page)
                        self._focus_scan_page(page)
                    else:
                        self._restore_page_url(page, current_url, quiet=True)
                        self._focus_scan_page(page)
                    break
                if target_page is not page:
                    self._log(f"[browser] Click opened a secondary page/modal: {label}")
                    self._close_extra_page(target_page)
                    self._focus_scan_page(page)
                elif self._strip_fragment(page.url) != self._strip_fragment(current_url):
                    self._restore_page_url(page, current_url, quiet=True)
                    self._focus_scan_page(page)
                else:
                    self._log(f"[browser] Click completed without navigation: {label}")
            except Exception:
                if self._should_stop():
                    raise RuntimeError("Scan stopped")
                if self._is_non_navigation_ui_action(action):
                    self._log(f"[browser] UI interaction did not navigate: {self._link_label(href or '', text)}")
                    continue
                click_failures += 1
                try:
                    for candidate in self._context_pages(context):
                        if candidate is not page:
                            self._close_extra_page(candidate)
                    if not self._page_is_closed(page) and self._strip_fragment(page.url) != self._strip_fragment(current_url):
                        self._restore_page_url(page, current_url, quiet=True)
                    self._focus_scan_page(page)
                except Exception:
                    pass
                continue

        if actions:
            parts = [f"clicked {clicked}"]
            if skipped_duplicate:
                parts.append(f"skipped {skipped_duplicate} duplicate/queued")
            if skipped_low_value:
                parts.append(f"skipped {skipped_low_value} low-value")
            if skipped_unresolved:
                parts.append(f"skipped {skipped_unresolved} unresolved")
            if click_failures:
                parts.append(f"{click_failures} click attempts did not complete")
            self._log(f"[browser] Action summary: {', '.join(parts)}")
            if click_failures:
                report.findings.append(
                    Finding(
                        category="navigation_failure",
                        message=f"{click_failures} visible action click attempt(s) did not complete",
                        url=current_url,
                        evidence=[self.evidence_for_url(current_url, note="click attempts did not complete")],
                    )
                )
    def _restore_page_url(self, page: Page, current_url: str, quiet: bool = False) -> bool:
        expected_url = self._strip_fragment(current_url)
        self._raise_if_stopped()
        try:
            if self._strip_fragment(page.url) == expected_url:
                return True
        except Exception:
            self._raise_if_stopped()
            return False

        restore_attempts = [
            ("back", getattr(page, "go_back", None)),
            ("goto", getattr(page, "goto", None)),
        ]
        for method, operation in restore_attempts:
            self._raise_if_stopped()
            if not callable(operation):
                continue
            try:
                if method == "back":
                    try:
                        operation(wait_until="domcontentloaded", timeout=min(self.timeout_seconds * 1000, 5000))
                    except TypeError:
                        operation(wait_until="domcontentloaded")
                else:
                    try:
                        operation(current_url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
                    except TypeError:
                        operation(current_url, wait_until="domcontentloaded")
                self._raise_if_stopped()
                try:
                    page.wait_for_timeout(POST_NAVIGATION_PAUSE_MS)
                except Exception:
                    self._raise_if_stopped()
                if self._strip_fragment(page.url) == expected_url:
                    self._dismiss_interruptions(page)
                    return True
            except Exception as exc:
                if self._should_stop():
                    raise RuntimeError("Scan stopped")
                if not quiet:
                    self._log(f"[browser] Could not restore page with {method}: {exc}")
        self._raise_if_stopped()
        if not quiet:
            self._log(f"[browser] Page restore failed; visible page is {getattr(page, 'url', 'unknown')}")
        return False

    def _browser_action_links(self, page: Page, base_url: str, current_url: str, clicked_seen: Set[str], clicked_action_keys: Set[str] | None = None) -> list[dict]:
        try:
            candidates = page.evaluate(
                """
                (baseUrl) => {
                  const sameOrigin = (href) => {
                    try { return new URL(href, window.location.href).origin === new URL(baseUrl).origin; } catch { return false; }
                  };
                  const visible = (el) => !!(el && el.getClientRects().length > 0);
                  const textFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.alt || '').trim();
                  const areaFor = (el) => {
                    const closest = (selector) => !!el.closest(selector);
                    const rect = el.getBoundingClientRect();
                    if (closest('nav, [role="navigation"], [id*="nav-xshop" i], [class*="nav-xshop" i], [class*="main-nav" i], [class*="menu" i]')) return 'nav';
                    if (closest('header, [role="banner"], [id*="header" i], [id="navbar"], [class*="header" i]') || rect.top < 120) return 'header';
                    if (closest('[class*="hero" i], [id*="hero" i], [class*="banner" i], [id*="banner" i], [class*="carousel" i], [class*="slider" i], [aria-roledescription="carousel"]')) return 'hero';
                    if (closest('[class*="card" i], [class*="tile" i], article, [class*="grid" i]')) return 'card';
                    if (closest('[class*="carousel" i], [class*="slider" i], [aria-roledescription="carousel"]')) return 'carousel';
                    if (closest('main, [role="main"]')) return 'main';
                    if (closest('footer, [role="contentinfo"]')) return 'footer';
                    return 'other';
                  };
                  const selector = [
                    'a[href]',
                    'button',
                    '[role="button"]',
                    '[role="link"]',
                    'input[type="submit"]'
                  ].join(',');
                  const out = [];
                  for (const el of document.querySelectorAll(selector)) {
                    if (!visible(el)) continue;
                    const rawHref = el.href || el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('formaction') || '';
                    let href = '';
                    if (rawHref) {
                      try { href = new URL(rawHref, window.location.href).href; } catch {}
                    }
                    if (href && !sameOrigin(href)) continue;
                    const text = textFor(el);
                    if (!text || text.length > 80) continue;
                    const rect = el.getBoundingClientRect();
                    out.push({
                      kind: el.tagName.toLowerCase() === 'a' ? 'link' : 'control',
                      text,
                      href,
                      area: areaFor(el),
                      top: Math.max(0, Math.round(rect.top + window.scrollY)),
                      left: Math.max(0, Math.round(rect.left + window.scrollX)),
                    });
                  }
                  return out;
                }
                """,
                base_url,
            )
        except Exception:
            candidates = []

        links: list[dict] = []
        seen_action_keys: set[str] = set(clicked_action_keys or set())
        for item in candidates:
            if not isinstance(item, dict):
                continue
            href = item.get("href")
            text = str(item.get("text") or "").strip()
            if not text or len(text) > 80:
                continue
            if self._is_excluded_planner_action(item):
                continue
            if href is not None and href != "" and (not isinstance(href, str) or not self._is_http_url(href)):
                continue
            normalized = self._strip_fragment(href) if isinstance(href, str) and href else ""
            if normalized and (normalized == current_url or normalized in clicked_seen):
                continue
            if normalized and self._is_low_priority_noise_url(normalized):
                continue
            action_key = self._action_key(item) if not normalized else normalized
            if action_key in seen_action_keys:
                continue
            seen_action_keys.add(action_key)
            copied = dict(item)
            if normalized:
                copied["href"] = normalized
            links.append(copied)
        return self._plan_actions(links)

        back = getattr(page, "go_back", None)
        if callable(back):
            try:
                back(wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
                return
            except TypeError:
                try:
                    back(wait_until="domcontentloaded")
                    return
                except Exception:
                    pass
            except Exception:
                pass

        try:
            page.goto(current_url, wait_until="load", timeout=self.timeout_seconds * 1000)
        except TypeError:
            try:
                page.goto(current_url, wait_until="load")
            except Exception:
                pass
        except Exception:
            pass

    def _resolve_action_locator(self, page: Page, action, href: str | None, text: str):
        candidates = []
        if href:
            candidates.append(page.locator(f'a[href^="{self._css_escape(href.split("#", 1)[0])}"]').first)
        if action.get("kind") == "link":
            candidates.append(page.get_by_text(text, exact=False).first)
        else:
            candidates.extend([
                page.get_by_role("button", name=text, exact=False).first,
                page.get_by_text(text, exact=False).first,
            ])
        for locator in candidates:
            if locator is not None:
                return locator
        return None

    @staticmethod
    def _context_pages(context: BrowserContext) -> list[Page]:
        pages = getattr(context, "pages", None)
        if callable(pages):
            try:
                return list(pages())
            except Exception:
                return []
        if isinstance(pages, list):
            return list(pages)
        return []

    def _close_extra_page(self, page: Page) -> None:
        if self._page_is_closed(page):
            return
        try:
            page.close()
        except Exception:
            self._raise_if_stopped()

    def _focus_scan_page(self, page: Page) -> None:
        if self._page_is_closed(page):
            return
        bring_to_front = getattr(page, "bring_to_front", None)
        if callable(bring_to_front):
            try:
                bring_to_front()
            except Exception:
                self._raise_if_stopped()

    def _is_risky_action_link(self, href: str | None, text: str, base_url: str) -> bool:
        normalized_text = " ".join(text.lower().replace("_", " ").replace("-", " ").split())
        compact_text = normalized_text.replace(" ", "")
        if normalized_text in AUTH_ACTION_TEXT or compact_text in AUTH_ACTION_TEXT:
            return False
        if not href or not self._is_http_url(href):
            return False
        parsed = urlparse(href)
        base_host = urlparse(base_url).netloc.lower()
        host = parsed.netloc.lower()
        host_parts = {part for part in host.replace("-", ".").split(".") if part}
        path_parts = {part.lower() for part in parsed.path.replace("-", "/").replace("_", "/").split("/") if part}
        if path_parts & AUTH_PATH_PARTS:
            return True
        if host != base_host and (host_parts & AUTH_HOST_PARTS):
            return True
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if {"client_id", "redirect_uri", "response_type"} & query_keys and (path_parts & {"oauth", "authorize", "auth"}):
            return True
        return False

    def _click_and_capture_popup(self, page: Page, locator) -> Page | None:
        self._raise_if_stopped()
        expect_popup = getattr(page, "expect_popup", None)
        if callable(expect_popup):
            try:
                with expect_popup(timeout=POPUP_WAIT_TIMEOUT_MS) as popup_info:
                    locator.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True)
                self._raise_if_stopped()
                popup = getattr(popup_info, "value", None)
                return popup
            except Exception:
                self._raise_if_stopped()
        try:
            self._raise_if_stopped()
            locator.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True)
        except Exception:
            self._raise_if_stopped()
            try:
                locator.evaluate("el => el.click()", timeout=ACTION_TIMEOUT_MS)
            except TypeError:
                locator.evaluate("el => el.click()")
            self._raise_if_stopped()
        return None

    @staticmethod
    def _page_is_closed(page: Page) -> bool:
        checker = getattr(page, "is_closed", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    @staticmethod
    def _css_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _scroll_page(self, page: Page) -> None:
        self._raise_if_stopped()
        self._log("[browser] Scroll start")
        page.evaluate(
            """
            async () => {
              const doc = document.scrollingElement || document.documentElement || document.body;
              const total = Math.max(
                doc.scrollHeight || 0,
                document.documentElement?.scrollHeight || 0,
                document.body?.scrollHeight || 0
              );
              const step = Math.max(window.innerHeight * 0.8, 500);
              for (let y = 0; y < total; y += step) {
                window.scrollTo(0, y);
                await new Promise((resolve) => requestAnimationFrame(() => resolve()));
              }
              window.scrollTo(0, 0);
            }
            """
        )
        self._raise_if_stopped()
        self._log("[browser] Scroll complete")

    def _capture_error_screenshot(self, page: Page, url: str, suffix: str) -> str:
        safe_suffix = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in suffix)[:40]
        with NamedTemporaryFile(prefix="qa-error-", suffix=f"-{safe_suffix}.png", delete=False) as tmp:
            path = tmp.name
        try:
            page.screenshot(path=path, full_page=True)
        except Exception:
            return ""
        return path

    def _capture_browser_evidence(self, page: Page, url: str, note: str | None = None) -> Evidence:
        screenshot_path = self._capture_error_screenshot(page, url, "evidence")
        return self.evidence_for_url(url, screenshot_path=screenshot_path, note=note)

    @staticmethod
    def _capture_recording_path(page: Page) -> str:
        video = getattr(page, "video", None)
        if video is None:
            return ""
        path = getattr(video, "path", None)
        if path is None:
            return ""
        try:
            return path() if callable(path) else str(path)
        except Exception:
            return ""

    def _record_request_failure(self, network_errors: list[str], request) -> None:
        host = urlparse(request.url).netloc.lower()
        if host in IGNORED_NETWORK_HOSTS:
            return
        failure_text = str(request.failure or "").lower()
        if any(marker in failure_text for marker in IGNORED_FAILURE_MARKERS):
            return
        if request.resource_type in {"image", "media", "font", "stylesheet", "script"} and "aborted" in failure_text:
            return
        network_errors.append(f"{request.method} {request.url}: {request.failure or 'request failed'}")

    def _classify_browser_error(self, message: str) -> str:
        lowered = message.lower()
        if ("console" in lowered or "error" in lowered) and "failed to load resource" not in lowered:
            return "js_error"
        if "failed to load resource" in lowered or "banner" in lowered:
            return self._resource_failure_category(message)
        if "http" in lowered or "net::" in lowered:
            return self._network_failure_category(message)
        return "api_failure"

    def _resource_failure_category(self, message: str) -> str:
        lowered = message.lower()
        if any(host in lowered for host in IGNORED_NETWORK_HOSTS):
            return "third_party_failure"
        if any(host in lowered for host in ("google-analytics.com", "googletagmanager.com", "doubleclick.net", "facebook.com", "hotjar.com")):
            return "third_party_failure"
        return "resource_failure"

    def _network_failure_category(self, message: str) -> str:
        lowered = message.lower()
        if any(host in lowered for host in IGNORED_NETWORK_HOSTS):
            return "third_party_failure"
        if any(token in lowered for token in ("google-analytics.com", "googletagmanager.com", "doubleclick.net", "facebook.com", "hotjar.com")):
            return "third_party_failure"
        if any(token in lowered for token in ("storage.googleapis.com", "/assets/", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".mp4")):
            return "resource_failure"
        return "api_failure"

    def _insert_next_links(self, queue: list[tuple[str, str | None]], links: Iterable[str], parent_url: str, seen: Set[str]) -> list[str]:
        queued_keys = {self._duplicate_key(item[0]) for item in queue}
        seen_keys = {self._duplicate_key(url) for url in seen}
        section_counts = self._section_counts([item[0] for item in queue] + list(seen))
        inserted: list[str] = []
        inserted_keys: set[str] = set()
        remaining_slots = max(0, min(MAX_NEW_LINKS_PER_PAGE, MAX_PAGES - len(seen) - len(queue)))
        ranked_links = self._rank_links(links, parent_url)
        has_primary_or_normal_links = any(self._link_priority_tier(link) <= 1 for link in ranked_links)
        for link in ranked_links:
            if len(inserted) >= remaining_slots:
                break
            tier = self._link_priority_tier(link)
            if has_primary_or_normal_links and tier >= 3:
                continue
            normalized = self._canonicalize_url(link)
            key = self._duplicate_key(normalized)
            if key in seen_keys or key in queued_keys or key in inserted_keys:
                continue
            if self._section_is_saturated(normalized, section_counts, parent_url):
                continue
            inserted.append(normalized)
            inserted_keys.add(key)
            section_counts[self._top_section(normalized)] = section_counts.get(self._top_section(normalized), 0) + 1
        if inserted:
            queue[0:0] = [(link, parent_url) for link in inserted]
        return inserted

    def _discover_links(self, base_url: str, page: _PageResult, seen: Set[str]) -> list[str]:
        discovered: list[str] = []
        discovered_keys: set[str] = set()
        page_key = self._duplicate_key(page.url)
        for link in page.links:
            absolute = urljoin(page.url, link)
            if not self._is_candidate_page_url(absolute, base_url):
                continue
            normalized = self._canonicalize_url(absolute)
            key = self._duplicate_key(normalized)
            if key != page_key and normalized not in seen and key not in discovered_keys:
                discovered.append(normalized)
                discovered_keys.add(key)
        return self._rank_links(discovered, page.url)

    @staticmethod
    def _top_section(url: str) -> str:
        path_parts = [part.lower() for part in urlparse(url).path.strip("/").split("/") if part]
        return path_parts[0] if path_parts else "__root__"

    @staticmethod
    def _path_depth(url: str) -> int:
        return len([part for part in urlparse(url).path.strip("/").split("/") if part])

    def _section_counts(self, urls: Iterable[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for url in urls:
            section = self._top_section(url)
            counts[section] = counts.get(section, 0) + 1
        return counts

    def _section_is_saturated(self, url: str, section_counts: dict[str, int], parent_url: str | None = None) -> bool:
        section = self._top_section(url)
        depth = self._path_depth(url)
        if section == "__root__":
            return False
        parent_section = self._top_section(parent_url) if parent_url else "__root__"
        limit = MAX_LINKS_PER_TOP_SECTION if section == parent_section else MAX_DEEP_LINKS_PER_TOP_SECTION if depth > 1 else MAX_LINKS_PER_TOP_SECTION
        return section_counts.get(section, 0) >= limit

    @staticmethod
    def _duplicate_key(url: str) -> str:
        parsed = urlparse(Phase1Tester._canonicalize_url(url))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, ""))

    def _fetch_page(self, url: str) -> _PageResult:
        request = Request(url, headers={"User-Agent": "AutonomousQA/1.0"})
        started = perf_counter()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if self._is_http_url(final_url) and not self._is_public_http_url(final_url):
                    return _PageResult(url=url, status=None, duration_seconds=perf_counter() - started, html="", links=[], text="", error="Blocked redirect to a private or internal network target")
                body = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            return _PageResult(url=url, status=exc.code, duration_seconds=perf_counter() - started, html="", links=[], text="", error=str(exc))
        except URLError as exc:
            return _PageResult(url=url, status=None, duration_seconds=perf_counter() - started, html="", links=[], text="", error=str(exc))

        parser = _PageParser()
        parser.feed(body)
        return _PageResult(
            url=url,
            status=status,
            duration_seconds=perf_counter() - started,
            html=body,
            links=parser.links,
            text=" ".join(parser.text_chunks),
            title=" ".join(parser.title_chunks).strip(),
        )

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        kept_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=False):
            lowered = key.lower()
            if lowered in TRACKING_QUERY_KEYS or any(lowered.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
                continue
            kept_query.append((key, value))
        return urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(kept_query, doseq=True), ""))

    @staticmethod
    def _url_terms(url: str) -> set[str]:
        parsed = urlparse(url)
        normalized = f"{parsed.path} {parsed.query}".lower().replace("-", "/").replace("_", "/")
        normalized = normalized.replace("?", "/").replace("&", "/").replace("=", "/")
        return {part.strip() for part in normalized.replace(" ", "/").split("/") if part.strip()}

    @classmethod
    def _link_priority_tier(cls, url: str) -> int:
        terms = cls._url_terms(url)
        path = urlparse(url).path.lower().replace("-", "/").replace("_", "/")
        path_parts = {part for part in path.split("/") if part}
        product_path = bool(path_parts & {"dp", "p", "pr", "item", "items"})
        search_or_journey_path = bool(path_parts & {"search", "cart", "basket", "bag", "checkout", "deal", "deals", "offer", "offers"})
        legal_or_special_path = any(
            marker in path
            for marker in (
                "privacy", "terms", "condition", "legal", "policy", "sitemap",
                "payment", "shipping", "cancellation", "refund", "gift",
                "flight", "travel", "mobile/apps", "corporate", "compliance",
                "security", "searchsuggestion",
            )
        )
        if product_path or search_or_journey_path or terms & (HIGH_PRIORITY_LINK_TERMS - {"search"}):
            return 0
        if terms & (LEGAL_FOOTER_LINK_TERMS | SPECIAL_SERVICE_LINK_TERMS) or legal_or_special_path or cls._is_low_priority_noise_url(url):
            return 3
        if terms & SUPPORT_LINK_TERMS:
            return 2
        if terms & CONTENT_LINK_TERMS:
            return 2
        if "search" in terms:
            return 0
        if terms & UTILITY_LINK_TERMS:
            return 3
        return 1

    @staticmethod
    def _is_low_priority_noise_url(url: str) -> bool:
        parsed = urlparse(url)
        normalized_path = parsed.path.lower().replace("-", "/").replace("_", "/")
        noise_tokens = (
            "footer", "wishlist", "safety", "alert", "auto/deliver", "/r", "create/invitation",
            "business", "gift/card", "shop/info", "flights", "flight", "travel", "outlet", "discover", "showroom",
            "corporate", "helpcentre", "help/centre", "sitemap", "mobile/apps", "pages/payments",
            "searchsuggestion", "cancellation", "shipping", "security", "compliance", "privacy",
        )
        useful_tokens = ("cart", "product", "deal", "offer", "search", "/s", "/dp", "/p/", "/pr", "bestseller", "category")
        return any(token in normalized_path for token in noise_tokens) and not any(token in normalized_path for token in useful_tokens)

    def _is_candidate_page_url(self, url: str, base_url: str) -> bool:
        if not self._is_http_url(url) or not self._is_crawlable_page(url):
            return False
        parsed = urlparse(url)
        if parsed.netloc != urlparse(base_url).netloc:
            return False
        normalized_path = parsed.path.lower().replace("-", "/").replace("_", "/")
        path_parts = {part.lower() for part in normalized_path.split("/") if part}
        if normalized_path.rstrip("/").endswith("/searchsuggestion"):
            return False
        if self._is_low_priority_noise_url(url):
            return False
        if path_parts & LOW_VALUE_PATH_PARTS:
            return False
        if path_parts & LOW_VALUE_LINK_PATH_PARTS and not (path_parts & HIGH_VALUE_PATH_PARTS):
            return False
        if path_parts & AUTH_PATH_PARTS:
            return False
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if query_keys and query_keys <= LOW_VALUE_QUERY_KEYS:
            return False
        if parsed.path.rstrip("/").lower().endswith("/get"):
            return False
        canonical = self._canonicalize_url(url)
        canonical_query_keys = {key.lower() for key, _ in parse_qsl(urlparse(canonical).query, keep_blank_values=True)}
        if parsed.query and not canonical_query_keys and not (path_parts & HIGH_VALUE_PATH_PARTS):
            return False
        return True

    def _rank_links(self, links: Iterable[str], parent_url: str | None = None) -> list[str]:
        unique: dict[str, str] = {}
        for link in links:
            canonical = self._canonicalize_url(link)
            unique.setdefault(self._duplicate_key(canonical), canonical)
        parent_section = self._top_section(parent_url) if parent_url else "__root__"

        def score(url: str) -> tuple[int, int, int, str]:
            parsed = urlparse(url)
            path_parts = {part.lower() for part in parsed.path.replace("-", "/").replace("_", "/").split("/") if part}
            section = self._top_section(url)
            tier = self._link_priority_tier(url)
            value = tier * 100
            if tier == 0 and path_parts & HIGH_VALUE_PATH_PARTS:
                value -= 20
            terms = self._url_terms(url)
            if terms & SUPPORT_LINK_TERMS:
                value += 35
            if terms & CONTENT_LINK_TERMS:
                value += 45
            if terms & (LEGAL_FOOTER_LINK_TERMS | SPECIAL_SERVICE_LINK_TERMS):
                value += 80
            if parent_section != "__root__" and section == parent_section:
                value -= 10
            elif parent_section != "__root__" and section != parent_section:
                value += 15
            if parsed.query:
                value += 6
            depth = len(path_parts)
            return (value, depth, len(url), url)

        return sorted(unique.values(), key=score)

    @staticmethod
    def _link_label(url: str, text: str = "") -> str:
        clean_text = " ".join(text.split())
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        if clean_text and len(clean_text) <= 48 and clean_text.lower() not in {"about", "products", "services", "learn more", "google", "home"}:
            return clean_text
        if not path_parts:
            return clean_text or "Home"
        useful_parts = path_parts[-2:] if len(path_parts) > 1 else path_parts
        label = " / ".join(
            " ".join(piece.capitalize() for piece in part.replace("-", " ").replace("_", " ").split())
            for part in useful_parts
        )
        return label or clean_text or parsed.netloc

    @staticmethod
    def _is_crawlable_page(url: str) -> bool:
        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        if path == "/":
            return True
        if any(path.endswith(ext) for ext in IGNORED_CRAWL_EXTENSIONS):
            return False
        last_segment = path.rsplit("/", 1)[-1]
        return "." not in last_segment

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            parsed = urlparse(f"https://{url}")
        if not parsed.path:
            parsed = parsed._replace(path="/")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))

    @staticmethod
    def _strip_fragment(url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, ""))

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"} or hostname.endswith((".localhost", ".local")):
            return False
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 80, type=socket.SOCK_STREAM)}
        except socket.gaierror:
            return False
        return bool(addresses) and all(not (ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_link_local or ipaddress.ip_address(address).is_reserved or ipaddress.ip_address(address).is_multicast or ipaddress.ip_address(address).is_unspecified) for address in addresses)

    @staticmethod
    def _is_http_url(url: str) -> bool:
        return urlparse(url).scheme in {"http", "https"}

    @staticmethod
    def _looks_like_js_error(html: str) -> bool:
        lowered = html.lower()
        return any(marker in lowered for marker in ("uncaught typeerror", "referenceerror", "syntaxerror", "javascript error"))

    @staticmethod
    def _looks_like_missing_element(text: str) -> bool:
        lowered = text.lower()
        return len(lowered.split()) < 5

    @staticmethod
    def _looks_like_api_failure(url: str, html: str, status: Optional[int]) -> bool:
        lowered = f"{url} {html}".lower()
        return bool(status and status >= 400 and any(token in lowered for token in ("/api/", "graphql", "fetch", "xhr")))

    @staticmethod
    def _should_check_missing_element(url: str, text: str) -> bool:
        return False

    def _log(self, message: str) -> None:
        self.logger(message)

    @staticmethod
    def _format_queue(queue: list[str], limit: int = 8) -> str:
        if not queue:
            return "(empty)"
        items = queue[:limit]
        formatted: list[str] = []
        for item in items:
            url = item[0] if isinstance(item, tuple) and item else str(item)
            formatted.append(Phase1Tester._link_label(str(url)))
        if len(queue) > limit:
            formatted.append(f"... (+{len(queue) - limit} more)")
        return " | ".join(formatted)

    @staticmethod
    def _format_actions(actions, limit: int = 8) -> str:
        if not actions:
            return "(none)"
        items: list[str] = []
        for action in actions[:limit]:
            if not isinstance(action, dict):
                continue
            text = str(action.get("text") or "").strip()
            href = str(action.get("href") or "").strip()
            area = str(action.get("area") or "other").strip() or "other"
            items.append(f"{Phase1Tester._link_label(href, text)} [{area}]")
        if len(actions) > limit:
            items.append(f"... (+{len(actions) - limit} more)")
        return " | ".join(items) if items else "(none)"

    @staticmethod
    def absolute_url(base_url: str, path: str) -> str:
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

    @staticmethod
    def evidence_for_url(url: str, screenshot_path: str | None = None, note: str | None = None) -> Evidence:
        return Evidence(kind="url", value=url, screenshot_path=screenshot_path, note=note)
