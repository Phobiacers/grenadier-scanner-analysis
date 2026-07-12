"""Playwright-backed transport for the INEOS forum ("stile" challenge bypass).

A headless Chromium fingerprint is used to fetch forum pages. It is launched lazily
and reused across navigations, handling stile challenge interstitials by polling for redirects.
"""
from __future__ import annotations

import random
import time
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

STILE_CHALLENGE_MARKER = "/.stile/challenge"
_CHALLENGE_TITLES = ("checking your browser", "just a moment", "please wait")
_CONTENT_SELECTOR = "div.structItem--thread, article.message, li.block-row"

@dataclass
class BrowserResponse:
    """A tiny requests.Response look-alike."""
    text: str
    status_code: int
    url: str

    def json(self):
        import json
        return json.loads(self.text)

class BrowserClient:
    """Headless-Chromium HTTP transport with PoliteClient-compatible `.get()`."""

    def __init__(self) -> None:
        self.cookies = os.environ.get("AO_FORUM_COOKIES", "")
        self.user_agent = os.environ.get(
            "AO_FORUM_BROWSER_UA",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        self.delay = float(os.environ.get("AO_FORUM_DELAY", "1.5"))
        self.max_delay = float(os.environ.get("AO_FORUM_DELAY_MAX", "4.0"))
        self.long_pause_chance = float(os.environ.get("AO_FORUM_LONG_PAUSE_CHANCE", "0.10"))
        self.long_pause_range = (
            float(os.environ.get("AO_FORUM_LONG_PAUSE_MIN", "8.0")),
            float(os.environ.get("AO_FORUM_LONG_PAUSE_MAX", "20.0"))
        )
        self.nav_timeout_ms = int(os.environ.get("AO_FORUM_NAV_TIMEOUT_MS", "45000"))
        self.content_wait_ms = int(os.environ.get("AO_FORUM_CONTENT_WAIT_MS", "6000"))
        self.max_retries = max(1, int(os.environ.get("AO_MAX_RETRIES", "3")))
        
        base_url = os.environ.get("THREAD_URL", "https://www.theineosforum.com")
        host = urlparse(base_url).hostname or ""
        self.cookie_domain = "." + host.split("www.", 1)[-1] if host else ""

        self._last_request = 0.0
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._failed = False

        # Silent-failure diagnostics
        self.challenge_hits = 0
        self.challenge_recovered = False

    def _cookie_dicts(self) -> list[dict]:
        out: list[dict] = []
        for part in self.cookies.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            out.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": self.cookie_domain,
                "path": "/",
            })
        return out

    def _ensure_browser(self) -> bool:
        if self._page is not None:
            return True
        if self._failed:
            return False
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            print(f"browserclient: Playwright not available ({exc}); forum fetch disabled.")
            self._failed = True
            return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            self._context.set_default_navigation_timeout(self.nav_timeout_ms)
            self._context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if self.cookies and self.cookie_domain:
                try:
                    self._context.add_cookies(self._cookie_dicts())
                    print(f"browserclient: seeded {self.cookies.count('=')} forum cookie(s).")
                except Exception as exc:
                    print(f"browserclient: could not set cookies ({exc}).")
            self._page = self._context.new_page()
            return True
        except Exception as exc:
            print(f"browserclient: failed to launch Chromium ({exc}); forum fetch disabled.")
            self._failed = True
            self.close()
            return False

    def close(self) -> None:
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def __enter__(self) -> BrowserClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _next_gap(self) -> float:
        if self.max_delay and self.max_delay > self.delay:
            gap = random.uniform(self.delay, self.max_delay)
        else:
            gap = self.delay
        if self.long_pause_chance and random.random() < self.long_pause_chance:
            gap += random.uniform(*self.long_pause_range)
        return gap

    def _throttle(self) -> None:
        gap = self._next_gap()
        elapsed = time.monotonic() - self._last_request
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def _on_challenge(self) -> bool:
        try:
            if STILE_CHALLENGE_MARKER in (self._page.url or ""):
                return True
            title = (self._page.title() or "").strip().lower()
            return any(t in title for t in _CHALLENGE_TITLES)
        except Exception:
            return False

    def _poll_until_cleared(self) -> bool:
        polls = max(1, self.nav_timeout_ms // 1500)
        for _ in range(polls):
            if not self._on_challenge():
                return True
            try:
                self._page.wait_for_timeout(1500)
            except Exception:
                break
        return not self._on_challenge()

    def get(self, url: str, **_kwargs) -> Optional[BrowserResponse]:
        """Navigate to `url` and return a BrowserResponse, or None on failure."""
        if not self._ensure_browser():
            return None

        hit_challenge = False
        for attempt in range(self.max_retries):
            self._throttle()
            status: Optional[int] = None
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                status = resp.status if resp else None
            except Exception as exc:
                self._last_request = time.monotonic()
                wait = (attempt + 1) * 5
                print(f"  browser nav error for {url} ({exc}); retry in {wait}s")
                time.sleep(wait)
                continue
            self._last_request = time.monotonic()

            if self._on_challenge():
                hit_challenge = True
                print(f"  stile challenge active at {self._page.url}; waiting for auto-solve...")
                if not self._poll_until_cleared():
                    wait = (attempt + 1) * 5
                    print(f"  challenge not cleared for {url}; backing off {wait}s and retrying")
                    time.sleep(wait)
                    continue

            try:
                self._page.wait_for_selector(_CONTENT_SELECTOR, timeout=self.content_wait_ms)
            except Exception:
                pass

            code = status if status is not None else 200
            
            if code == 404:
                # XenForo returns 404 when we paginate past the end of a thread
                html = self._page.content()
                final_url = self._page.url
                return BrowserResponse(text=html, status_code=404, url=final_url)

            if code == 429 or code >= 500:
                wait = (attempt + 1) * 5
                print(f"  HTTP {code} for {url}; backing off {wait}s")
                time.sleep(wait)
                continue
            
            if 400 <= code < 500:
                print(f"  HTTP {code} for {url}; not retrying.")
                return None

            html = self._page.content()
            final_url = self._page.url
            if hit_challenge:
                self.challenge_hits += 1
                self.challenge_recovered = True
            return BrowserResponse(text=html, status_code=code, url=final_url)

        if hit_challenge:
            self.challenge_hits += 1
        print(f"  giving up on {url}")
        return None
