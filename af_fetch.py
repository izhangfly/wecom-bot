#!/usr/bin/env python3
"""Fetch airfleets.net airframe data for a registration (or MSN / airline).

Run under the infographic venv, inside Xvfb (a real display clears the light
Cloudflare challenge that headless/datacenter alone does not):
    xvfb-run -a /root/infographic-maker/venv/bin/python3 af_fetch.py B-2479

Prints ONE json line: {"ok": true, "url": ..., "text": ...} or {"ok": false, "error": ...}

Flow: /recherche/?key=<q> → first /ficheapp/plane-*.htm link (the reg row) →
the aircraft page → extract the spec block ("Serial number" … Hex/Engines/Status)
plus the "Operators of the aircraft" history table, which is exactly what the
owner pastes today. airfleets uses Cloudflare's *auto-clearing* managed challenge
(unlike planespotters' hard PoW), so a real browser passes it even on the server;
the cf_clearance cookie is cached in a persistent profile between runs.
"""
import json
import os
import random
import re
import sys
import time

PROFILE = os.environ.get("AF_PROFILE", "/root/wecom-bot/.af_profile")
CHALLENGE_WAIT_S = int(os.environ.get("AF_CHALLENGE_WAIT", "45"))
CHANNEL = os.environ.get("AF_CHANNEL", "chromium")  # 'chrome' on a Mac (dev/test)


def fail(msg):
    print(json.dumps({"ok": False, "error": str(msg)[:300]}))
    sys.exit(0)


def _pause(a=0.4, b=1.1):
    """Short human-like pause — enough to look organic, short enough not to keep
    the owner waiting on WeCom (the whole fetch is ~1 min already)."""
    time.sleep(random.uniform(a, b))


def _dismiss_consent(page):
    """Click 'Agree' on the 'We value your privacy' cookie CMP (may be in an
    iframe). Best-effort: returns quietly if there's nothing to dismiss."""
    labels = ["AGREE", "Agree", "I Agree", "Accept all", "Accept All", "Accept",
              "Agree and close", "同意", "同意并关闭"]
    for fr in [page] + list(page.frames):
        for lab in labels:
            for loc in (lambda: fr.get_by_role("button", name=lab, exact=False),
                        lambda: fr.locator(f"text=\"{lab}\"")):
                try:
                    el = loc()
                    if el.count():
                        el.first.click(timeout=1500)
                        _pause(0.3, 0.7)
                        return True
                except Exception:
                    pass
    return False


def _wait_cleared(page, needle=None):
    for _ in range(max(1, CHALLENGE_WAIT_S // 2)):
        try:
            _dismiss_consent(page)
            t = page.title().lower()
            if "moment" not in t and "loading" not in t:
                if needle is None or page.get_by_text(needle, exact=False).count():
                    return True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return False


def main():
    if len(sys.argv) < 2:
        fail("usage: af_fetch.py <registration|msn|airline>")
    q = sys.argv[1].strip()
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, channel=CHANNEL, headless=False, no_viewport=True, locale="en-GB")
        page = ctx.new_page()
        page.goto("https://www.airfleets.net/recherche/?key=" + q,
                  wait_until="domcontentloaded", timeout=45000)
        _pause(0.6, 1.4)  # settle like a human before reading the results
        if not _wait_cleared(page):
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            ctx.close()
            fail(f"search page never cleared the challenge (title={title!r})")

        # airfleets' OWN anti-bot captcha (/home/captcha.php, "not a robot") trips
        # after too many rapid searches from one IP — detect it and bail cleanly
        # so the caller falls back to manual paste instead of a confusing result.
        if "captcha" in page.url.lower() or "not a robot" in page.inner_text("body").lower():
            ctx.close()
            fail("airfleets rate-limit captcha hit (too many rapid searches) — "
                 "use manual paste, or try again in a while")

        # the reg row links to /ficheapp/plane-*.htm; take the first real one
        links = page.eval_on_selector_all(
            "a", "els=>els.filter(a=>/\\/ficheapp\\/plane-/.test(a.href)).map(a=>a.href)")
        if not links:
            ctx.close()
            fail(f"no aircraft found on airfleets for {q!r} "
                 "(unknown reg, or airfleets blocked the search)")
        plane_url = links[0]

        _pause(0.5, 1.2)  # brief hesitation before clicking through, like a person
        page.goto(plane_url, wait_until="domcontentloaded", timeout=45000)
        _pause(0.5, 1.2)
        _dismiss_consent(page)
        # wait for the JS-loaded content: the operators table text is the signal
        _wait_cleared(page, needle="Operators of the aircraft")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        url = page.url
        body = page.inner_text("body")
        ctx.close()

    m = re.search(r"Serial number", body)
    start = m.start() if m else 0
    # keep through the operators history; cut at the Photos/Gallery boilerplate
    end = -1
    for marker in ("PHOTOS", "Photos of", "\nPhotos\n", "RADAR LINK",
                   "Gallery", "Add a photo", "Discuss"):
        i = body.find(marker, start)
        if i > 0:
            end = i if end < 0 else min(end, i)
    if end < 0 or end - start > 4000:
        end = min(len(body), start + 3000)
    text = re.sub(r"\n{3,}", "\n\n", body[start:end]).strip()
    if len(text) < 80:
        text = body[:4000]
    # Fail LOUDLY if the page doesn't look like a real airframe record — a silent
    # airfleets layout change must fall back to manual paste, never feed DeepSeek
    # garbage. Require the reg (or its de-hyphenated form) plus 2+ spec anchors.
    low = text.lower()
    regforms = {q.lower(), q.lower().replace("-", ""), q.lower().replace(" ", "")}
    has_reg = any(f and f in low.replace("-", "").replace(" ", "") for f in regforms)
    anchors = sum(k in low for k in ("serial number", "type", "first flight",
                                     "hex code", "engines", "status", "operators",
                                     "airline", "msn"))
    if anchors < 2 or not has_reg:
        fail(f"airfleets page didn't look like an airframe record "
             f"(anchors={anchors}, reg_seen={has_reg}) — layout may have changed; use manual paste")
    print(json.dumps({"ok": True, "url": url, "text": text[:6000]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        fail(e)
