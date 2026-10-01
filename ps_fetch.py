#!/usr/bin/env python3
"""Fetch planespotters production-list / airframe data for a registration.

Run with the infographic-maker venv python (has patchright + chromium):
    xvfb-run -a /root/infographic-maker/venv/bin/python3 ps_fetch.py B-2479

Prints ONE json line:  {"ok": true, "url": ..., "text": ...}
                  or:  {"ok": false, "error": ...}

planespotters sits behind Cloudflare's *managed challenge*. On a residential IP
+ a genuine browser it auto-clears invisibly; on a datacenter IP it escalates to
an interactive proof-of-work page (/challenge?crid=…) that NO automation clears.
So this only succeeds when routed through a residential/mobile IP — set PS_PROXY
(e.g. http://user:pass@host:port). Uses patchright (a de-fingerprinted Playwright)
to satisfy the invisible-JS variant, waits the challenge out, then dismisses the
cookie-consent banner ("Accept & Close") before reading the page. Photo links
(/photo/…) are never followed — owner ruling keeps photos out of this leg.
"""
import json
import os
import re
import sys

STATE_PROFILE = os.environ.get("PS_PROFILE", "/root/wecom-bot/.ps_profile")
PROXY = os.environ.get("PS_PROXY", "").strip()
CHALLENGE_WAIT_S = int(os.environ.get("PS_CHALLENGE_WAIT", "45"))


def fail(msg):
    print(json.dumps({"ok": False, "error": str(msg)[:300]}))
    sys.exit(0)


def _dismiss_cookie(page):
    for sel in ("button:has-text('Accept & Close')", "text=Accept & Close",
                "button:has-text('Accept')"):
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2000)
                return
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        fail("usage: ps_fetch.py <registration>")
    reg = sys.argv[1].strip()
    try:
        from patchright.sync_api import sync_playwright  # patched, low-fingerprint
    except ImportError:
        from playwright.sync_api import sync_playwright

    launch = dict(channel="chromium", headless=False, no_viewport=True, locale="en-GB")
    if PROXY:
        launch["proxy"] = {"server": PROXY}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(STATE_PROFILE, **launch)
        page = ctx.new_page()
        page.goto("https://www.planespotters.net/search?q=" + reg,
                  wait_until="domcontentloaded", timeout=45000)

        # wait out the managed challenge; dismiss the cookie banner as it appears
        ready = False
        for _ in range(max(1, CHALLENGE_WAIT_S // 2)):
            _dismiss_cookie(page)
            try:
                if page.query_selector("a[href*='/airframe/']"):
                    ready = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(2000)
        if not ready:
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            ctx.close()
            fail(f"verification page never cleared (title={title!r}) — "
                 f"datacenter IP is challenged; a residential PS_PROXY is required")

        if "/airframe/" not in page.url:
            href = page.get_attribute("a[href*='/airframe/']", "href")
            if href and href.startswith("/"):
                href = "https://www.planespotters.net" + href
            if href:
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
                _dismiss_cookie(page)
        try:
            page.wait_for_selector("text=Manufacturer Serial Number", timeout=30000)
        except Exception:
            page.wait_for_timeout(3000)
        url = page.url
        body = page.inner_text("body")
        ctx.close()

    m = re.search(re.escape(reg), body, re.I)
    start = m.start() if m else 0
    end = body.find("AIRFRAME PHOTOS")
    if end < 0:
        end = len(body)
    cut = body[start:end].strip()
    if len(cut) < 200:
        cut = body[:8000]
    print(json.dumps({"ok": True, "url": url, "text": cut[:8000]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — must always emit the json line
        fail(e)
