from __future__ import annotations

from urllib.parse import urlparse

from .scanner_config import (
    ACTION_TIMEOUT_MS, MAX_ACTIONS_PER_PAGE, HIGH_VALUE_ACTION_TERMS, HIGH_VALUE_PATH_PARTS,
    LOW_VALUE_ACTION_TERMS, NON_NAV_UI_TERMS, PAGE_AREA_PRIORITY,
    PRIMARY_ACTION_TERMS, PRODUCT_METADATA_LABELS, RETRY_ACTION_TERMS,
    REVIEW_LABEL_TERMS, SECONDARY_ACTION_TERMS, LEGAL_FOOTER_LINK_TERMS, SECTION_ACTION_LIMITS,
    SECTION_TEST_ORDER, VARIANT_ACTION_TERMS, WEAK_ACTION_LABELS,
)


class ActionPlannerMixin:
    @staticmethod
    def _normalized_action_text(text: str) -> str:
        return " ".join(text.lower().replace("_", " ").replace("-", " ").split())

    @classmethod
    def _is_critical_action(cls, action: dict) -> str | None:
        text = cls._normalized_action_text(str(action.get("text") or ""))
        href = str(action.get("href") or "").lower().replace("-", " ").replace("_", " ")
        combined = f"{text} {href}"
        if cls._is_low_value_action_text(text):
            return False
        return any(term in combined for term in PRIMARY_ACTION_TERMS | {"deals", "bestseller", "bestsellers"})

    @classmethod
    def _is_low_value_action_text(cls, text: str) -> str | None:
        normalized = cls._normalized_action_text(text)
        if normalized.isdigit() or (normalized.startswith("(") and normalized.endswith(")")):
            return True
        if "colour" in normalized or "color" in normalized or "pattern" in normalized:
            return True
        if normalized.startswith("b0") and len(normalized) > 6:
            return True
        if "/ ref=" in normalized or " ref=" in normalized or "title:" in normalized or "linktext:" in normalized:
            return True
        if normalized.startswith("₹") or "free delivery" in normalized or "cash on delivery" in normalized:
            return True
        if len(normalized) <= 1:
            return True
        if normalized in WEAK_ACTION_LABELS:
            return True
        if any(label in normalized for label in PRODUCT_METADATA_LABELS):
            return True
        if any(term in normalized for term in RETRY_ACTION_TERMS):
            return True
        if normalized.startswith("best ") and not any(term in normalized for term in ("best seller", "best deal", "best price", "best offer")):
            return True
        tokens = set(normalized.split())
        if tokens & VARIANT_ACTION_TERMS and not any(term in normalized for term in ("add", "buy", "cart", "checkout")):
            return True
        if tokens & REVIEW_LABEL_TERMS:
            return True
        if any(normalized.endswith(suffix) for suffix in (" kg pouch", " g pouch", " ml", " cm", " mm")):
            return True
        return normalized in LOW_VALUE_ACTION_TERMS

    @classmethod
    def _is_non_navigation_ui_action(cls, action: dict) -> str | None:
        text = cls._normalized_action_text(str(action.get("text") or ""))
        area = str(action.get("area") or "")
        href = str(action.get("href") or "")
        if href:
            return False
        return area == "carousel" or any(term in text for term in NON_NAV_UI_TERMS)

    @classmethod
    def _action_key(cls, action: dict) -> str:
        text = cls._normalized_action_text(str(action.get("text") or ""))
        area = str(action.get("area") or "other")
        href = str(action.get("href") or "")
        if href:
            parsed = urlparse(href)
            path = parsed.path.rstrip("/") or "/"
            return f"href:{path}:{text}"
        return f"text:{text}"

    @classmethod
    def _is_excluded_planner_action(cls, action: dict) -> str | None:
        area = str(action.get("area") or "")
        text_terms = set(cls._normalized_action_text(str(action.get("text") or "")).split())
        if area == "footer":
            return False
        text = cls._normalized_action_text(str(action.get("text") or ""))
        href = cls._normalized_action_text(str(action.get("href") or ""))
        combined = f"{text} {href}"
        protected = ("add to cart", "buy now", "checkout", "place order", "search", "category", "categories")
        if any(term in combined for term in protected):
            return False
        review_or_metadata = (
            "product review", "product-reviews", "reviews/", "/review", "rating",
            "customer support", "cash on delivery", "no cash on delivery",
            "brand store", "visit brand store", "retry", "try again",
        )
        if any(term in combined for term in review_or_metadata):
            return True
        if text in {"next", "previous", "prev", "first", "last"} or text.startswith("page "):
            return True
        return cls._is_low_value_action_text(text) and not cls._is_critical_action(action)

    def _plan_actions(self, actions: list[dict]) -> list[dict]:
        """Select useful actions in DOM order, grouped by page container."""
        grouped: dict[str, list[dict]] = {area: [] for area in SECTION_TEST_ORDER}
        seen_keys: set[str] = set()
        for action in actions:
            if self._is_excluded_planner_action(action):
                continue
            area = self._action_area_label(action)
            key = self._action_key(action)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            grouped.setdefault(area, []).append(action)

        planned: list[dict] = []
        for area in SECTION_TEST_ORDER:
            limit = SECTION_ACTION_LIMITS.get(area, 0)
            if limit > 0:
                planned.extend(grouped.get(area, [])[:limit])
        return planned

    def _rank_actions(self, actions: list[dict]) -> list[dict]:
        def score(action: dict) -> tuple[int, int, int, int, int, str]:
            text = str(action.get("text") or "")
            href = str(action.get("href") or "")
            area = str(action.get("area") or "other")
            combined = f"{text} {href}".lower().replace("-", " ").replace("_", " ")
            value = 0
            if self._is_non_navigation_ui_action(action):
                value += 60
            if any(term in combined for term in PRIMARY_ACTION_TERMS):
                value -= 70
            elif any(term in combined for term in SECONDARY_ACTION_TERMS):
                value -= 40
            elif any(term in combined for term in HIGH_VALUE_ACTION_TERMS):
                value -= 20
            else:
                value += 25
            if self._is_low_value_action_text(text):
                value += 120
            if href:
                parsed = urlparse(href)
                path_parts = {part.lower() for part in parsed.path.replace("-", "/").replace("_", "/").split("/") if part}
                if path_parts & HIGH_VALUE_PATH_PARTS:
                    value -= 25
                if parsed.query:
                    value += 3
            if area == "footer":
                value += 180
            text_length_penalty = 0 if len(text) <= 32 else 8
            top = int(action.get("top") or 0)
            return (PAGE_AREA_PRIORITY.get(area, PAGE_AREA_PRIORITY["other"]), value + text_length_penalty, top // 400, len(text), int(action.get("left") or 0), text.lower())

        return sorted(actions, key=score)

    @staticmethod
    def _action_area_label(action: dict) -> str:
        area = str(action.get("area") or "other")
        return area if area in PAGE_AREA_PRIORITY else "other"

    @staticmethod
    def _highlight_action(locator, label: str) -> str | None:
        try:
            locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
            locator.evaluate(
                """
                (el, text) => {
                  const old = document.getElementById('qa-scanner-highlight');
                  if (old) old.remove();
                  const oldOverlay = document.getElementById('qa-scanner-focus-overlay');
                  if (oldOverlay) oldOverlay.remove();
                  el.dataset.qaScannerHighlight = 'true';
                  el.style.setProperty('outline', '3px solid #ffb000', 'important');
                  el.style.setProperty('outline-offset', '4px', 'important');
                  el.style.setProperty('box-shadow', '0 0 0 6px rgba(255, 176, 0, 0.28), 0 0 18px rgba(255, 176, 0, 0.65)', 'important');
                  el.style.setProperty('position', el.style.position || 'relative', 'important');
                  el.style.setProperty('z-index', '2147483647', 'important');
                  const r = el.getBoundingClientRect();
                  const pad = 10;
                  const x1 = Math.max(0, r.left - pad);
                  const y1 = Math.max(0, r.top - pad);
                  const x2 = Math.min(window.innerWidth, r.right + pad);
                  const y2 = Math.min(window.innerHeight, r.bottom + pad);
                  const overlay = document.createElement('div');
                  overlay.id = 'qa-scanner-focus-overlay';
                  Object.assign(overlay.style, {
                    position: 'fixed', left: x1 + 'px', top: y1 + 'px', width: Math.max(1, x2 - x1) + 'px', height: Math.max(1, y2 - y1) + 'px', zIndex: '2147483646', pointerEvents: 'none',
                    background: 'transparent',
                    boxShadow: '0 0 0 9999px rgba(8, 15, 30, 0.68)',
                    transition: 'opacity 120ms ease'
                  });
                  document.body.appendChild(overlay);
                  const badge = document.createElement('div');
                  badge.id = 'qa-scanner-highlight';
                  badge.textContent = `Testing: ${text}`;
                  Object.assign(badge.style, {
                    position: 'fixed', zIndex: '2147483647', pointerEvents: 'none',
                    background: '#172033', color: '#fff', padding: '6px 10px',
                    borderRadius: '6px', font: '600 12px system-ui, sans-serif',
                    boxShadow: '0 3px 12px rgba(0,0,0,.35)', maxWidth: 'min(360px, 80vw)',
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'
                  });
                  document.body.appendChild(badge);
                  badge.style.left = `${Math.max(8, Math.min(window.innerWidth - badge.offsetWidth - 8, r.left))}px`;
                  badge.style.top = `${Math.max(8, r.top - badge.offsetHeight - 10)}px`;
                }
                """, label[:120]
            )
            return "ok"
        except Exception as exc:
            return str(exc)

    @staticmethod
    def _clear_action_highlight(page: Page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  const el = document.querySelector('[data-qa-scanner-highlight="true"]');
                  if (el) {
                    el.style.removeProperty('outline');
                    el.style.removeProperty('outline-offset');
                    el.style.removeProperty('box-shadow');
                    el.style.removeProperty('position');
                    el.style.removeProperty('z-index');
                    delete el.dataset.qaScannerHighlight;
                  }
                  document.getElementById('qa-scanner-highlight')?.remove();
                  document.getElementById('qa-scanner-focus-overlay')?.remove();
                }
                """
            )
        except Exception:
            pass

