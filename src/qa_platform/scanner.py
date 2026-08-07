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
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .models import Evidence, Finding, PageSummary, TestReport

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
except Exception:  # pragma: no cover - optional dependency
    Browser = BrowserContext = Page = object  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


MAX_PAGES = 30
MAX_ACTIONS_PER_PAGE = 8
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
class Phase1Tester:
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
                break
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
                for link in self._discover_links(base_url, page, seen):
                    queue.append((link, normalized))
                self._log(f"Discovered {len(queue)} queued pages after: {normalized}")

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
        queue: list[tuple[str, str | None]] = [(base_url, None)]
        context = self._open_browser_context(browser)
        self._set_runtime(browser=browser, context=context, page=None)
        console_errors: list[str] = []
        network_errors: list[str] = []
        page = self._open_scan_page(context, console_errors, network_errors)
        self._set_runtime(browser=browser, context=context, page=page)
        try:
            while queue and len(seen) < MAX_PAGES:
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
                    self._log(f"[browser] Visiting page {len(seen)}/{MAX_PAGES}: {normalized}")
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
                    for link in dom_links + nav_links + links:
                        if len(seen) + len(queue) >= MAX_PAGES:
                            break
                        if not self._is_crawlable_page(link):
                            continue
                        if link not in seen and link not in [item[0] for item in queue]:
                            queue.append((link, normalized))
                    if dom_links or nav_links or links:
                        self._log(f"[browser] Queue now ({len(queue)}): {self._format_queue(queue)}")
                    action_links = [] if self.fast_browser else self._browser_action_links(page, base_url, normalized, clicked_seen)
                    self._log(
                        f"[browser] Clickable links on page ({len(action_links)}): {self._format_actions(action_links)}"
                    )
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

                if action_links:
                    self._exercise_visible_actions(report, context, page, normalized, action_links, seen, queue, base_url, clicked_seen)
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
        if self.headless:
            return browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 720},
            )
        return browser.new_context(
            ignore_https_errors=True,
            no_viewport=True,
        )

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

    def _exercise_visible_actions(self, report: TestReport, context: BrowserContext, page: Page, current_url: str, actions, seen: Set[str], queue, base_url: str, clicked_seen: Set[str] | None = None) -> None:
        if clicked_seen is None:
            clicked_seen = set()
        count = 0
        for action in actions:
            self._raise_if_stopped()
            if count >= MAX_ACTIONS_PER_PAGE:
                break
            href = action.get("href")
            text = str(action.get("text") or "").strip()
            if not text or len(text) > 80:
                continue
            try:
                self._log(f"[browser] Clicking link: {text} -> {href or 'unknown'}")
                locator = self._resolve_action_locator(page, action, href, text)
                if locator is None:
                    self._log(f"[browser] Skipping unresolved link: {text}")
                    continue
                self._dismiss_interruptions(page)
                before_url = page.url
                before_pages = self._context_pages(context)
                popup_page = self._click_and_capture_popup(page, locator)
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
                    self._log(f"[browser] Target page closed while clicking: {text}")
                    return
                if not self.fast_browser:
                    self._scroll_page(target_page)
                after_url = self._strip_fragment(target_page.url)
                if after_url != before_url and self._is_http_url(after_url) and urlparse(after_url).netloc == urlparse(base_url).netloc:
                    self._log(f"[browser] Click resolved to: {after_url}")
                    queued_urls = {item[0] if isinstance(item, tuple) else item for item in queue}
                    if after_url not in seen and after_url not in queued_urls:
                        queue.append((after_url, current_url))
                    if after_url not in clicked_seen:
                        clicked_seen.add(after_url)
                        report.clicked_urls.append(after_url)
                    self._log(f"[browser] Queue updated ({len(queue)}): {self._format_queue(queue)}")
                    if target_page is not page:
                        try:
                            target_page.close()
                        except Exception:
                            pass
                    else:
                        self._restore_page_url(page, current_url)
                    return
                count += 1
            except Exception as exc:
                if self._should_stop() or str(exc) == "Scan stopped":
                    raise RuntimeError("Scan stopped")
                restored = False
                try:
                    if not self._page_is_closed(page) and self._strip_fragment(page.url) != self._strip_fragment(current_url):
                        restored = self._restore_page_url(page, current_url)
                except Exception:
                    restored = False
                if restored:
                    self._log(f"[browser] Recovered original page after click error: {current_url}")
                if "Target page, context or browser has been closed" in str(exc):
                    self._log(f"[browser] Click aborted because page closed: {text}")
                    return
                self._log(f"[browser] Click failed on {current_url}: {text} ({exc})")
                report.findings.append(
                    Finding(
                        category="navigation_failure",
                        message=f"Visible action failed on {current_url}: {text} ({exc})",
                        url=current_url,
                        evidence=[self.evidence_for_url(current_url, note=str(exc))],
                    )
                )
    def _restore_page_url(self, page: Page, current_url: str) -> bool:
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
                self._log(f"[browser] Could not restore page with {method}: {exc}")
        self._raise_if_stopped()
        self._log(f"[browser] Page restore failed; visible page is {getattr(page, 'url', 'unknown')}")
        return False

    def _browser_action_links(self, page: Page, base_url: str, current_url: str, clicked_seen: Set[str]) -> list[dict]:
        try:
            candidates = page.evaluate(
                """
                (baseUrl) => {
                  const sameOrigin = (href) => {
                    try { return new URL(href).origin === new URL(baseUrl).origin; } catch { return false; }
                  };
                  const visible = (el) => !!(el && el.getClientRects().length > 0);
                  const candidates = [
                    ...document.querySelectorAll('a[href]')
                  ];
                  const out = [];
                  for (const el of candidates) {
                    if (!visible(el)) continue;
                    const href = el.href;
                    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                    if (!text || text.length > 80) continue;
                    if (!href || !sameOrigin(href)) continue;
                    out.push({
                      kind: 'link',
                      text,
                      href,
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
        for item in candidates:
            if not isinstance(item, dict):
                continue
            href = item.get("href")
            text = str(item.get("text") or "").strip()
            if not text or len(text) > 80:
                continue
            if href is not None and (not isinstance(href, str) or not self._is_http_url(href)):
                continue
            normalized = self._strip_fragment(href) if isinstance(href, str) else ""
            if normalized and normalized != current_url and normalized not in clicked_seen and normalized not in [str(link.get("href") or "") for link in links]:
                links.append(item)
        return links

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
        if action.get("kind") == "link" and href:
            candidates.extend(
                [
                    page.get_by_text(text, exact=False).first,
                    page.locator(f'a[href^="{self._css_escape(href.split("#", 1)[0])}"]').first,
                ]
            )
        else:
            candidates.append(page.get_by_text(text, exact=False).first)
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

    def _discover_links(self, base_url: str, page: _PageResult, seen: Set[str]) -> list[str]:
        discovered: list[str] = []
        for link in page.links:
            absolute = urljoin(page.url, link)
            if not self._is_http_url(absolute) or not self._is_crawlable_page(absolute):
                continue
            if urlparse(absolute).netloc != urlparse(base_url).netloc:
                continue
            normalized = self._strip_fragment(absolute)
            if normalized != page.url and normalized not in seen and normalized not in discovered:
                discovered.append(normalized)
        return discovered

    @staticmethod
    def _duplicate_key(url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

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

    @staticmethod
    def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
        seen: set[tuple[str, str | None, str]] = set()
        unique: list[Finding] = []
        for finding in findings:
            fingerprint = (finding.category, Phase1Tester._strip_fragment(Phase1Tester._normalize_url_like(finding.url or "")), finding.message)
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

    @staticmethod
    def _build_phase2_summary(report: TestReport, findings: list[Finding]) -> str:
        unique_slow_pages = len(Phase1Tester._unique_slow_pages(findings))
        return (
            f"Site score: {report.site_score}/100 using weighted category scores. "
            f"Risk level: {report.risk_level}. "
            f"Unique findings: {len(findings)}. "
            f"Deduplicated from {report.total_findings} total findings. "
            f"Slow pages raw: {report.slow_pages}. "
            f"Slow pages unique: {unique_slow_pages}."
        )

    @staticmethod
    def _build_executive_summary(report: TestReport, findings: list[Finding]) -> str:
        slow_pages = Phase1Tester._unique_slow_pages(findings)
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

    @staticmethod
    def _unique_slow_pages(findings: list[Finding]) -> list[tuple[str, float]]:
        unique: dict[str, tuple[str, float]] = {}
        for finding in findings:
            if finding.category != "slow_page" or not finding.url:
                continue
            canonical = Phase1Tester._normalize_url_like(finding.url)
            duration = Phase1Tester._extract_duration(finding.message)
            current = unique.get(canonical)
            if current is None or duration > current[1]:
                unique[canonical] = (Phase1Tester._friendly_page_label_from_url(finding.url), duration)
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
        if len(queue) > limit:
            items.append(f"... (+{len(queue) - limit} more)")
        formatted: list[str] = []
        for item in items:
            if isinstance(item, tuple) and item:
                formatted.append(str(item[0]))
            else:
                formatted.append(str(item))
        return " | ".join(formatted)

    @staticmethod
    def _format_actions(actions, limit: int = 8) -> str:
        if not actions:
            return "(none)"
        items: list[str] = []
        for action in actions[:limit]:
            if not isinstance(action, dict):
                continue
            text = str(action.get("text") or "").strip() or "untitled"
            href = str(action.get("href") or "").strip() or "unknown"
            items.append(f"{text} -> {href}")
        if len(actions) > limit:
            items.append(f"... (+{len(actions) - limit} more)")
        return " | ".join(items) if items else "(none)"

    @staticmethod
    def absolute_url(base_url: str, path: str) -> str:
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

    @staticmethod
    def evidence_for_url(url: str, screenshot_path: str | None = None, note: str | None = None) -> Evidence:
        return Evidence(kind="url", value=url, screenshot_path=screenshot_path, note=note)
