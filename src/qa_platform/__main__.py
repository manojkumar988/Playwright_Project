from __future__ import annotations

import argparse
import sys

from .reporting import format_raw_report
from .scanner import Phase1Tester


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous QA phase 1 tester")
    parser.add_argument("url", help="Target website URL")
    parser.add_argument(
        "--mode",
        choices=["auto", "browser", "browser-fast", "http"],
        default="auto",
        help="Use browser automation when available, force browser, force fast browser crawling, or force HTTP crawling",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright in headless mode instead of the default headed mode",
    )
    args = parser.parse_args()

    browser_mode = "browser" if args.mode == "browser-fast" else args.mode
    report = Phase1Tester(
        args.url,
        browser_mode=browser_mode,
        headless=args.headless,
        fast_browser=args.mode == "browser-fast",
    ).run()
    print(format_raw_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
