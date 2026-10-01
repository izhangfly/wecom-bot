#!/usr/bin/env python3
"""IZhang Publisher — WeCom bot V5.0, port 8642 behind Caddy.

Live flow (owner + shared pipeline DB at /root/infographic-maker/data/):
  article → send original profile image → Ground Shadow? → Drop Shadow? →
  confirm reg (loop) → auto-fetch planespotters airframe data (headless
  chromium, V5.4; manual paste stays as the fallback) →
  [async] DeepSeek synthesize → "DeepSeek compilation completed" →
  planespotters photo (fetch→send→delete; owner ruling 2026-07-13) →
  Bing images (8, ≥1000px, send full-size one-by-one, delete) →
  brew job queued (renderer = next build) → TITLE msg → BODY msg →
  "Article published?" → yes = mark Published.

Global rule: any reply containing "skip" → discard-confirm. WeCom needs an
answer in 5s, so heavy legs run in daemon threads + active sends.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from flask import Flask, request, make_response

from wecom_crypto import WeComCrypto, WeComCryptoError

# ----------------------------------------------------------------- config
def load_env(path="/root/wecom-bot/wecom.env"):
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
CORP_ID = os.environ["WECOM_CORP_ID"]
AGENT_ID = int(os.environ.get("WECOM_AGENT_ID", "0"))
SECRET = os.environ["WECOM_SECRET"]
TOKEN = os.environ["WECOM_TOKEN"]
AES_KEY = os.environ["WECOM_AES_KEY"]
ALLOWED = {u.strip().lower() for u in
           os.environ.get("WECOM_ALLOWED_USERS", "").split(",") if u.strip()}
OWNER = (os.environ.get("WECOM_ALLOWED_USERS", "").split(",") or [""])[0].strip()

# outbound mail (Bing reference images go by email — WeCom can't multi-download)
MAIL_HOST = os.environ.get("MAIL_HOST", "smtp.163.com")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "994"))
MAIL_FROM = os.environ.get("MAIL_FROM", "")
MAIL_AUTH = os.environ.get("MAIL_AUTH", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
PUBLIC_SITE = "http://YOUR_SERVER_IP:8640"

SITE = "http://127.0.0.1:8640"
DATA_DB = "/root/infographic-maker/data/pipeline.db"
LOCAL_PREFIX = "/Users/ianzhang/livery-pipeline/output/"
SERVER_OUTPUT = "/root/infographic-maker/data/output"
BREW_DIR = "/root/infographic-maker/data/brew_jobs"
REFS_OUT = "/root/infographic-maker/data/refs"   # Bing images the site serves
TMP = "/root/wecom-bot/tmp"
BBID = "/root/wecom-bot/bbid/bbid/bbid.py"
UA = "IZhangPublisher/5.0 (+mailto:cjqdlc98@outlook.com)"
# airframe auto-fetch (V5.4) — real headful chromium under Xvfb, patchright.
# airfleets.net (af_fetch.py) clears its light Cloudflare challenge on the server,
# so auto-fetch is ON by default. planespotters (ps_fetch.py) needs a residential
# PS_PROXY (hard PoW on the datacenter IP) and stays manual-paste only.
AF_FETCH = "/root/wecom-bot/af_fetch.py"
AF_PYTHON = "/root/infographic-maker/venv/bin/python3"
AF_AUTOFETCH = os.environ.get("AF_AUTOFETCH", "1") == "1"

os.makedirs(BREW_DIR, exist_ok=True)
os.makedirs(TMP, exist_ok=True)

crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
app = Flask(__name__)
logging.basicConfig(filename="/root/wecom-bot/bot.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ------------------------------------------------------------------- data
def db():
    c = sqlite3.connect(DATA_DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")
    return c


def map_path(p):
    if not p:
        return None
    if p.startswith(LOCAL_PREFIX):
        p = os.path.join(SERVER_OUTPUT, p[len(LOCAL_PREFIX):])
    return p if os.path.exists(p) else None


def next_queued_entry():
    c = db()
    r = c.execute("""SELECT id, airline_name, registration, source_credit,
        license, article_title, article_path, image_path FROM images
        WHERE is_fictional=0 AND article_status='done'
          AND (published_status IS NULL OR published_status NOT IN ('published','discarded'))
        ORDER BY id LIMIT 1""").fetchone()
    c.close()
    return dict(r) if r else None


def mark_entry(rid, status):
    c = db()
    c.execute("UPDATE images SET published_status=? WHERE id=?", (status, rid))
    c.commit()
    c.close()
    log.info("entry %s -> %s", rid, status)


def mark_fictional(rid):
    """Owner spotted a fictional livery mid-flow: tag it, and discard the
    already-generated article (owner ruling — no articles for fictional)."""
    c = db()
    c.execute("""UPDATE images SET is_fictional=1, classify_status='done',
                 classify_method='human', classify_confidence=1.0,
                 article_status='skipped', published_status='discarded'
                 WHERE id=?""", (rid,))
    c.commit()
    c.close()
    log.info("entry %s -> fictional (article discarded)", rid)


def next_fictional_entry():
    c = db()
    r = c.execute("""SELECT id, airline_name, registration, source_credit,
        license, image_path FROM images
        WHERE is_fictional=1
          AND (published_status IS NULL OR published_status NOT IN ('published','discarded'))
          AND image_path IS NOT NULL
        ORDER BY id LIMIT 1""").fetchone()
    c.close()
    return dict(r) if r else None


# fields a job may mutate — snapshotted at job start so 'kill' can roll back
_SNAP_FIELDS = ("is_fictional", "classify_status", "classify_method",
                "classify_confidence", "article_status", "published_status",
                "registration")


def snapshot_entry(rid):
    c = db()
    r = c.execute(f"SELECT {','.join(_SNAP_FIELDS)} FROM images WHERE id=?",
                  (rid,)).fetchone()
    c.close()
    return dict(r) if r else None


def restore_entry(rid, snap):
    if not snap:
        return
    c = db()
    c.execute(f"UPDATE images SET {', '.join(f'{k}=?' for k in _SNAP_FIELDS)} WHERE id=?",
              [snap.get(k) for k in _SNAP_FIELDS] + [rid])
    c.commit()
    c.close()
    log.info("entry %s restored to pre-job snapshot", rid)


def status_text():
    """Comprehensive queue snapshot for the 'check status' button."""
    c = db()
    def n(q, *a):
        return c.execute(q, a).fetchone()[0]
    total = n("SELECT COUNT(*) FROM images")
    ocr_done = n("SELECT COUNT(*) FROM images WHERE ocr_status='done'")
    art_done = n("SELECT COUNT(*) FROM images WHERE article_status='done'")
    queueing = n("""SELECT COUNT(*) FROM images WHERE is_fictional=0 AND article_status='done'
                    AND (published_status IS NULL OR published_status NOT IN ('published','discarded'))""")
    published = n("SELECT COUNT(*) FROM images WHERE published_status='published'")
    discarded = n("SELECT COUNT(*) FROM images WHERE published_status='discarded'")
    skipped = n("SELECT COUNT(*) FROM images WHERE article_status='skipped'")
    fict = n("SELECT COUNT(*) FROM images WHERE is_fictional=1")
    fict_todo = n("""SELECT COUNT(*) FROM images WHERE is_fictional=1
                     AND (published_status IS NULL OR published_status NOT IN ('published','discarded'))
                     AND image_path IS NOT NULL""")
    real = n("SELECT COUNT(*) FROM images WHERE is_fictional=0 AND classify_status='done'")
    insuff = n("SELECT COUNT(*) FROM images WHERE article_status='insufficient'")
    c.close()
    lines = [
        "📊 IZhang Publisher — status",
        f"• Real articles queueing (ready to publish): {queueing}",
        f"• Published: {published}   · Discarded: {discarded}",
        f"• Fictional total: {fict}  (awaiting fa: {fict_todo})",
        f"• Real (classified): {real}   · Skipped: {skipped}   · Insufficient info: {insuff}",
        f"• Articles written: {art_done}",
        f"• OCR done: {ocr_done} / {total}",
    ]
    for fn in (removebg_quota_line, deepseek_balance_line):
        ln = fn()
        if ln:
            lines.append("• " + ln)
    return "\n".join(lines)


def kill_job(user):
    """Discard the current job and roll the backend back to the pre-job state."""
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    entry = d.get("entry") or {}
    snap = d.get("snapshot")
    restored = ""
    if entry.get("id") and snap:
        restore_entry(entry["id"], snap)
        restored = f" Entry #{entry['id']} rolled back to its pre-job state."
    SESS[user] = {"state": S_IDLE, "data": {}}
    save_sessions()
    def _after():
        send_text(user, f"✖ Current job discarded.{restored} Start your next article?")
        send_card(user, "Start your next article?", "Pick a workflow — or type a command.",
                  [("▶ Real Article", "article", 1), ("🎭 Fictional Article", "fa", 1)])
    bg(_after)
    return ""


def _strip_title(text, title):
    """Body must never repeat the title. Drop a leading title line whether it's
    a markdown H1 ('# …') or a bare first line matching the stored title."""
    text = (text or "").strip()
    lines = text.split("\n")
    if not lines:
        return text
    first = lines[0].lstrip("#").strip().strip("《》").strip()
    t = (title or "").strip().strip("《》").strip()
    if lines[0].startswith("#") or (t and first == t):
        text = "\n".join(lines[1:]).lstrip("\n")
    return text.strip()


def article_text(entry):
    title = (entry.get("article_title") or "").strip()
    p = map_path(entry.get("article_path"))
    if not p:
        return title, ""
    return title, _strip_title(open(p, encoding="utf-8").read(), title)


# ------------------------------------------------------- WeCom send API
_tok = {"t": 0.0, "v": None}


def access_token():
    if time.time() - _tok["t"] < 7000 and _tok["v"]:
        return _tok["v"]
    r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                     params={"corpid": CORP_ID, "corpsecret": SECRET},
                     timeout=15).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {r}")
    _tok.update(t=time.time(), v=r["access_token"])
    return r["access_token"]


def send_text(user, content):
    # WeCom text cap is 2048 BYTES (utf-8) — ~680 Chinese chars, not 2048 chars
    while len(content.encode("utf-8")) > 2000:
        content = content[:int(len(content) * 0.9)]
    r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                      params={"access_token": access_token()},
                      json={"touser": user, "msgtype": "text", "agentid": AGENT_ID,
                            "text": {"content": content}}, timeout=20).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"send_text failed: {r}")


def send_long_text(user, text, limit=1800):
    """Split by UTF-8 bytes at natural boundaries so nothing gets truncated."""
    text = (text or "").strip()
    while text:
        chunk = text
        while len(chunk.encode("utf-8")) > limit:
            chunk = chunk[:int(len(chunk) * 0.9)]
        if len(chunk) < len(text):  # break at a paragraph / sentence if we can
            cut = max(chunk.rfind("\n"), chunk.rfind("。"),
                      chunk.rfind("！"), chunk.rfind("？"))
            if cut > len(chunk) // 2:
                chunk = chunk[:cut + 1]
        send_text(user, chunk)
        text = text[len(chunk):].lstrip("\n")


def send_media(user, path, prefer="image"):
    """Upload + send. Images >2MB (WeCom cap) fall back to file messages so
    full resolution always arrives."""
    size = os.path.getsize(path)
    mtype = "image" if (prefer == "image" and size <= 2 * 1024 * 1024) else "file"
    with open(path, "rb") as f:
        up = requests.post("https://qyapi.weixin.qq.com/cgi-bin/media/upload",
                           params={"access_token": access_token(), "type": mtype},
                           files={"media": (os.path.basename(path), f)},
                           timeout=60).json()
    if up.get("errcode") != 0:
        raise RuntimeError(f"media upload failed: {up}")
    r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                      params={"access_token": access_token()},
                      json={"touser": user, "msgtype": mtype, "agentid": AGENT_ID,
                            mtype: {"media_id": up["media_id"]}}, timeout=30).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"send_media failed: {r}")


def bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def send_card(user, title, desc, buttons, task_id=None):
    """Interactive template card: preset buttons the owner can TAP, while still
    being able to type a free-text reply. buttons = [(text, key), …]; each key
    is routed through handle() exactly as if the owner had typed it.
    Falls back to a plain text message if the card API errors."""
    task_id = task_id or f"t{int(time.time()*1000)%100000000}"
    card = {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc or ""},
        "task_id": task_id,
        "button_list": [{"text": b[0], "style": (b[2] if len(b) > 2 else 1),
                         "key": b[1]} for b in buttons[:6]],
    }
    try:
        r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                          params={"access_token": access_token()},
                          json={"touser": user, "msgtype": "template_card",
                                "agentid": AGENT_ID, "template_card": card},
                          timeout=20).json()
        if r.get("errcode") != 0:
            raise RuntimeError(r)
    except Exception as e:
        log.warning("send_card fell back to text: %s", e)
        opts = "  ·  ".join(b[0] for b in buttons)
        send_text(user, f"{title}\n{desc or ''}\n[{opts}]  (tap or type)")


def send_multi_card(user, title, desc, selects, submit_key, submit_text="Confirm ✅"):
    """Multiple-selection card: several dropdown questions on ONE card + a submit
    button, so the owner answers the opening questions together instead of one
    after another. selects = [(question_key, question_title, [(id,text),…], default_id)].
    On submit WeCom sends a template_card_event with SelectedItems. Returns True
    if the card was sent, else False so the caller can fall back to sequential cards."""
    card = {
        "card_type": "multiple_interaction",
        "main_title": {"title": title, "desc": desc or ""},
        "task_id": f"m{int(time.time()*1000)%100000000}",
        "select_list": [{
            "question_key": qk, "title": qt, "selected_id": default,
            "option_list": [{"id": oid, "text": otext} for oid, otext in opts],
        } for qk, qt, opts, default in selects],
        "submit_button": {"text": submit_text, "key": submit_key},
    }
    try:
        r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                          params={"access_token": access_token()},
                          json={"touser": user, "msgtype": "template_card",
                                "agentid": AGENT_ID, "template_card": card},
                          timeout=20).json()
        if r.get("errcode") != 0:
            raise RuntimeError(r)
        return True
    except Exception as e:
        log.warning("send_multi_card failed (%s) — caller should fall back", e)
        return False


def deepseek_balance_line():
    """One-line DeepSeek balance from the account API (empty on failure)."""
    try:
        key = _deepseek_key()
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {key}",
                                  "Accept": "application/json"}, timeout=15).json()
        infos = r.get("balance_infos") or []
        if not infos:
            return ""
        # pick the funded currency (account holds USD + CNY separately)
        def _f(x):
            try:
                return float(x.get("total_balance") or 0)
            except (TypeError, ValueError):
                return 0.0
        b = max(infos, key=_f)
        avail = "" if r.get("is_available", True) else " ⚠️ unavailable"
        return (f"💧 DeepSeek balance: {b.get('total_balance','?')} "
                f"{b.get('currency','')}{avail}")
    except Exception:
        return ""


def _deepseek_key():
    for p in ("/root/infographic-maker/deepseek.key", "/root/wecom-bot/deepseek.key"):
        try:
            return open(p).read().strip()
        except Exception:
            continue
    return os.environ.get("DEEPSEEK_KEY", "")


CREATE_MENU_KEYS = {
    "MENU_ARTICLE": "article", "MENU_FA": "fa",
    "MENU_STATUS": "status", "MENU_KILL": "kill", "MENU_DISCARD": "discard",
    "MENU_PUBLISHED": "published",
}


def create_menu():
    """Persistent bottom-menu buttons (reliable quick actions). Idempotent —
    menu/create overwrites. Keys route through handle() like typed commands."""
    menu = {"button": [
        {"type": "click", "name": "▶ Real", "key": "MENU_ARTICLE"},
        {"type": "click", "name": "🎭 Fictional", "key": "MENU_FA"},
        {"name": "More", "sub_button": [
            {"type": "click", "name": "📊 Check status", "key": "MENU_STATUS"},
            {"type": "click", "name": "✅ Mark published (P)", "key": "MENU_PUBLISHED"},
            {"type": "click", "name": "🗑 Discard this entry", "key": "MENU_DISCARD"},
            {"type": "click", "name": "✖ Kill current job", "key": "MENU_KILL"},
        ]},
    ]}
    try:
        r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/menu/create",
                          params={"access_token": access_token(), "agentid": AGENT_ID},
                          json=menu, timeout=15).json()
        log.info("create_menu: %s", r)
    except Exception as e:
        log.warning("create_menu: %s", e)


def send_email(subject, body, files):
    """SMTP-SSL via 163 (port 994; 465/587 are blocked on this host). 163 drops
    the connection mid-send when it's busy/rate-limiting → 'Server not connected';
    retry up to 3× with a FRESH connection each time so a transient drop recovers."""
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    for fp in files:
        with open(fp, "rb") as f:
            data = f.read()
        ext = os.path.splitext(fp)[1].lstrip(".").lower() or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        msg.add_attachment(data, maintype="image", subtype=ext,
                           filename=os.path.basename(fp))
    last = None
    # 163 drops the connection when it's throttling a burst; email is a background
    # leg here, so retry generously with growing backoff to ride out the window.
    for attempt, wait in enumerate((3, 8, 15, 25, 40)):
        try:
            with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=90) as s:
                s.login(MAIL_FROM, MAIL_AUTH)
                s.send_message(msg)
            return
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                smtplib.SMTPResponseException, OSError) as e:
            last = e
            log.warning("send_email attempt %s failed: %s", attempt + 1, e)
            time.sleep(wait)
    raise last


# ------------------------------------------------------- pipeline legs
def leg_planespotters_photo(user, reg):
    try:
        j = requests.get(f"https://api.planespotters.net/pub/photos/reg/{reg}",
                         headers={"User-Agent": UA}, timeout=20).json()
        photos = j.get("photos") or []
        if not photos:
            send_text(user, f"ℹ️ Planespotters has no photo for {reg} — moving on.")
            return
        ph = photos[0]
        url = (ph.get("thumbnail_large") or ph.get("thumbnail") or {}).get("src", "")
        credit = ph.get("photographer", "unknown")
        link = ph.get("link", "")
        d = os.path.join(TMP, f"ps-{int(time.time())}")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{reg}-planespotters.jpg")
        img = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        img.raise_for_status()
        open(fp, "wb").write(img.content)
        send_media(user, fp)
        send_text(user, f"📷 © {credit} — {link}")
        shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        log.warning("planespotters leg: %s", e)
        try:
            send_text(user, "⚠️ Planespotters image fetching is not working — moving on.")
        except Exception:
            pass


def pick_keyword(entry, dsjson):
    """Rationalized Bing keyword: operator name + friendly type = specific,
    image-rich, unambiguous. Falls back to OCR airline name."""
    if dsjson:
        a = (dsjson.get("airline_en") or "").strip()
        t = (dsjson.get("headline_en") or dsjson.get("type_en") or "").strip()
        if a:
            return f"{a} {t}".strip()
    return (entry.get("airline_name") or "").strip() or (entry.get("registration") or "")


def derive_keywords(entry, dsjson):
    """Owner (2026-07-14): once the article is written, derive 4 image-search
    keywords FROM the article's actual content — #1 the specific aircraft, the
    rest the people / related airlines / other types the article names — and
    search Bing with each. Uses DeepSeek over the article body; falls back to a
    heuristic list if it's unavailable."""
    anchor = pick_keyword(entry, dsjson)      # '<airline> <type>' — the aircraft
    title = body = ""
    try:
        title, body = article_text(entry)
    except Exception as e:
        log.warning("derive_keywords read: %s", e)
    if body:
        try:
            r = requests.post(f"{SITE}/api/keywords", json={
                "airline": entry.get("airline_name") or "",
                "registration": entry.get("registration") or "",
                "type_en": (dsjson or {}).get("type_en") or (dsjson or {}).get("headline_en") or "",
                "title": title, "body": body}, timeout=90)
            if r.ok:
                kws = [k.strip() for k in ((r.json() or {}).get("keywords") or []) if str(k).strip()]
                if kws:
                    if anchor and anchor.lower() not in [k.lower() for k in kws]:
                        kws = [anchor] + kws
                    return kws[:4]
        except Exception as e:
            log.warning("derive_keywords deepseek: %s", e)
    # heuristic fallback: aircraft anchor + airline + type + reg
    a = (entry.get("airline_name") or "").strip()
    reg = (entry.get("registration") or "").strip()
    t = ((dsjson or {}).get("type_en") or "").strip()
    out = []
    for k in (anchor, a, f"{a} {t}".strip(), reg):
        k = (k or "").strip()
        if k and k.lower() not in [x.lower() for x in out]:
            out.append(k)
    return out[:4] or [anchor or a or reg]


def leg_refs(user, entry, dsjson):
    """Background: derive keywords from the finished article, fetch refs, e-mail
    them, and THEN release the 'published?' check-in card (owner 2026-07-15: the
    card must come after the 📧 line, whether the mail succeeds, fails, or there
    were no images)."""
    try:
        leg_bing_images(user, derive_keywords(entry, dsjson), entry)
    finally:
        s = SESS.get(user) or {}
        d = s.get("data") or {}
        d.pop("await_refs", None)
        save_sessions()
        send_finish_card_if_ready(user)


def _compress_ref(src, dst, max_side=1600, q=85):
    """Reference photos are for the designer to look at / save, not to publish —
    normalize to a light JPEG so the page loads fast and the e-mail stays small
    enough that 163 doesn't drop the connection mid-send."""
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            s = max_side / max(w, h)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        im.save(dst, format="JPEG", quality=q, optimize=True)


def leg_bing_images(user, keywords, entry=None, per=4, min_side=900):
    """Fetch reference images for up to 4 content-derived keywords (one per key
    aspect of the article), compress them light, publish to the batch-download
    page and e-mail them as a best-effort bonus. The page link is the reliable
    route; the compressed attachments keep 163 from dropping the send."""
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [k for k in (str(x).strip() for x in (keywords or [])) if k][:4]
    if not keywords:
        return
    from PIL import Image
    d = os.path.join(TMP, f"bing-{int(time.time())}")
    os.makedirs(d, exist_ok=True)
    keep = []          # list of (filepath, keyword)
    seen_sizes = set()
    try:
        send_text(user, "🔎 Bing 参考图关键词（按文章内容）：\n"
                        + "\n".join(f"{i+1}. {k}" for i, k in enumerate(keywords)))
        for ki, kw in enumerate(keywords):
            sub = os.path.join(d, f"k{ki}")
            os.makedirs(sub, exist_ok=True)
            cmd = ["python3", BBID, kw, "-o", sub, "--limit", str(per * 3),
                   "--filters", "+filterui:imagesize-custom_900_900"]
            try:
                subprocess.run(cmd, capture_output=True, timeout=180)
            except Exception as e:
                log.warning("bbid '%s': %s", kw, e)
                continue
            got = 0
            for f in sorted(Path(sub).glob("*")):
                if got >= per or not f.is_file():
                    continue
                try:
                    with Image.open(f) as im:
                        if max(im.size) < min_side:
                            continue
                    sz = f.stat().st_size
                    if sz in seen_sizes:        # cheap cross-keyword dedup
                        continue
                    seen_sizes.add(sz)
                    keep.append((str(f), kw))
                    got += 1
                except Exception:
                    continue
        if not keep:
            send_text(user, "⚠️ Bing 没搜到合适的参考图（≥%dpx）。" % min_side)
            shutil.rmtree(d, ignore_errors=True)
            return
        airline = (entry or {}).get("airline_name") or keywords[0]
        reg = (entry or {}).get("registration") or ""
        eid = (entry or {}).get("id") or int(time.time())
        rdir = os.path.join(REFS_OUT, str(eid))
        compressed = []
        try:
            if os.path.isdir(rdir):
                shutil.rmtree(rdir, ignore_errors=True)
            os.makedirs(rdir, exist_ok=True)
            for i, (fp, kw) in enumerate(keep, 1):
                out = os.path.join(rdir, f"{i:02d}.jpg")
                try:
                    _compress_ref(fp, out)
                except Exception:
                    shutil.copy(fp, out)
                compressed.append(out)
            # prune: keep the newest 10 ref sets
            sets = sorted((x for x in os.listdir(REFS_OUT)
                           if os.path.isdir(os.path.join(REFS_OUT, x))),
                          key=lambda x: os.path.getmtime(os.path.join(REFS_OUT, x)))
            for old in sets[:-10]:
                shutil.rmtree(os.path.join(REFS_OUT, old), ignore_errors=True)
            send_text(user, f"🖼 {len(compressed)} 张 Bing 参考图（{airline} {reg}，来自 "
                            f"{len(keywords)} 组关键词）：\n{PUBLIC_SITE}/refs/{eid}\n\n"
                            "打开后点 SAVE ALL 一键存进相册（若在微信里点开无法保存，"
                            "用右上角『在 Safari 中打开』再存）。")
        except Exception as e:
            log.warning("refs publish: %s", e)
        # e-mail best-effort in the background, with the light compressed images
        subject = f"[IZhang 配图] {airline} {reg} — {len(compressed)} 张参考图"
        bodytext = (f"航司/涂装: {airline} {reg}\n关键词（{len(keywords)} 组）: "
                    f"{'、'.join(keywords)}\n共 {len(compressed)} 张，见附件。\n\n"
                    f"页面（可批量下载）：{PUBLIC_SITE}/refs/{eid}\n— IZhang Publisher 自动发送")
        atts = compressed or [fp for fp, _ in keep]
        # e-mail runs inline here — leg_refs already runs in the background, so
        # this doesn't block the main WeCom flow, and it lets the "published?"
        # check-in card fire AFTER the 📧 line (owner 2026-07-15).
        try:
            send_email(subject, bodytext, atts)
            send_text(user, f"📧 参考图也已邮寄至 {MAIL_TO}")
        except Exception as e:
            log.warning("bing email: %s", e)
            send_text(user, f"（邮件发送失败：{str(e)[:60]}，用上面的页面链接即可）")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return
    except Exception as e:
        log.warning("bing leg: %s", e)
        try:
            send_text(user, f"⚠️ Bing 参考图获取失败（{str(e)[:80]}）— moving on.")
        except Exception:
            pass
    shutil.rmtree(d, ignore_errors=True)


def removebg_quota_line():
    """One-line bg-removal quota readout for messages (empty string on failure)."""
    try:
        j = requests.get(f"{SITE}/api/removebg_quota", timeout=15).json()
        if j.get("quota_used") is None:
            return ""
        line = f"🧮 BG-removal quota (used/free): {j['quota_used']} / {j['quota_free']}"
        if j.get("balance") is not None:
            line += f" · balance ${j['balance']}"
        return line
    except Exception:
        return ""


def leg_autofetch_airframe(user, d, reg):
    """V5.4: fetch the airframe data ourselves. airfleets.net works on the server
    (light Cloudflare challenge a real browser clears); planespotters does NOT
    (hard PoW on the datacenter IP) and is manual-paste only. On success we go
    straight into DeepSeek; on ANY failure state stays S_RAW and the owner pastes.
    Never fights the human — if they've already pasted/moved on, we bail."""
    j = {}
    try:
        # xvfb-run gives chromium a real (virtual) display — headless is detected
        p = subprocess.run(["xvfb-run", "-a", AF_PYTHON, AF_FETCH, reg],
                           capture_output=True, text=True, timeout=140)
        lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        if lines:
            j = json.loads(lines[-1])
    except Exception as e:
        log.warning("af autofetch: %s", e)
    s = SESS.get(user) or {}
    if s.get("state") != S_RAW or (s.get("data") or {}).get("reg") != d.get("reg"):
        return  # owner already pasted or moved on — never fight the human
    if j.get("ok") and len(j.get("text", "")) >= 80:
        s["state"] = "processing"
        save_sessions()
        send_text(user, f"✅ Auto-fetched the airframe data ({len(j['text'])} chars) "
                        f"from airfleets.net — no paste needed. Processing…")
        leg_deepseek_and_rest(user, d, j["text"])
    else:
        send_text(user, "⚠️ Auto-fetch didn't get usable data this time — paste the "
                        "planespotters (or airfleets) airframe text manually when ready "
                        f"({(j.get('error') or 'no data')[:100]}).")


def leg_deepseek_and_rest(user, sess_data, raw_text):
    """The long tail after the owner pastes planespotters text."""
    entry = sess_data["entry"]
    reg = sess_data.get("reg") or entry.get("registration") or ""
    dsjson = None
    # 1) DeepSeek
    try:
        r = requests.post(f"{SITE}/api/synthesize",
                          json={"registration": reg, "raw": raw_text},
                          timeout=200)
        if r.ok and "error" not in (r.json() or {}):
            dsjson = r.json()
            send_text(user, "🧪 DeepSeek compilation completed")
        else:
            send_text(user, f"⚠️ DeepSeek failed: {r.json().get('error','?')[:120]} — continuing without it.")
    except Exception as e:
        log.warning("deepseek: %s", e)
        try:
            send_text(user, "⚠️ DeepSeek request failed — continuing without it.")
        except Exception:
            pass
    # 2) brew job for the infographic renderer (renderer = next build step)
    job = {"entry_id": entry["id"], "reg": reg,
           "ground_shadow": sess_data.get("ground_shadow"),
           "drop_shadow": sess_data.get("drop_shadow"),
           "source_credit": entry.get("source_credit"),
           "license": entry.get("license"),
           "deepseek": dsjson, "created": time.time()}
    jp = os.path.join(BREW_DIR, f"{entry['id']}.json")
    json.dump(job, open(jp, "w"), ensure_ascii=False, indent=1)
    send_text(user, "🎨 Brewing your infographics… you'll receive a gallery "
                    "link here the moment the render finishes.")
    # 3) bing reference images: derive 4 content-based keywords from the finished
    # article, search each, publish to the refs page + email — all in the
    # BACKGROUND so nothing waits on keyword extraction or the slow SMTP send.
    bg(leg_refs, user, entry, dsjson)
    # 4) the article is NOT sent as chat messages anymore — it lives on the brew
    # gallery page in copyable title/body boxes (owner 2026-07-14).
    # 5) publish confirmation is HELD until the render finishes (brew_done fires
    # it), so the owner isn't asked "published?" before the gallery + refs arrive.
    SESS[user]["state"] = S_PUBLISHED
    SESS[user]["data"]["await_brew"] = entry["id"]
    SESS[user]["data"]["await_refs"] = entry["id"]   # card waits for the 📧 line too
    SESS[user]["data"].pop("finish_card_sent", None)
    save_sessions()
    bg(finish_card_fallback, user, entry["id"])      # guarantee the card arrives
    send_text(user, "⏳ 信息图渲染中，完成后给你带标题正文的图库链接，再问你是否发布。")


# ------------------------------------------------------- state machine
SESS: dict[str, dict] = {}
SEEN_MSG: dict[str, float] = {}

S_IDLE = "idle"
S_CONFIRM_DISCARD = "confirm_discard"
S_COMBO = "combo_open"        # combined opening card (fictional+ground+drop, reg typed)
S_FICTCHECK = "fict_check"
S_GROUND = "ground_shadow"
S_DROP = "drop_shadow"
S_REG = "confirm_reg"
S_RAW = "wait_raw"
S_PUBLISHED = "published_confirm"
# fictional-article (fa) flow
S_FCOMBO = "f_combo"          # combined fa opening card (ground+drop, reg typed)
S_FGROUND = "f_ground"
S_FDROP = "f_drop"
S_FREG = "f_reg"
S_FAIRLINE = "f_airline"
S_FTYPE = "f_type"
S_FDONE = "f_done"


def is_yes(t): return t.strip().lower() in ("yes", "y", "是", "对")
def is_no(t): return t.strip().lower() in ("no", "n", "否", "不")
def wants_skip(t): return "skip" in t.strip().lower()


def reprompt(state, d):
    return {S_FICTCHECK: "Is this livery fictional? (yes/no)",
            S_GROUND: "Add ground shadow? (yes/no)",
            S_DROP: "Add drop shadow? (yes/no)",
            S_REG: f"Confirm registration number is: {d.get('reg','')}",
            S_RAW: "Paste the planespotters airframe text when ready.",
            S_PUBLISHED: "Article published? (yes/no)",
            S_FGROUND: "Add ground shadow? (yes/no)",
            S_FDROP: "Add drop shadow? (yes/no)",
            S_FREG: f"Registration for this fictional livery: {d.get('reg','(none)')} — confirm (yes) or type the correct one",
            S_FAIRLINE: f"Airline name: {d.get('airline','(none)')} — confirm (yes) or type the correct one",
            S_FDONE: "Mark this fictional entry as handled/published? (yes/no)",
            }.get(state, "Send 'article' to start.")


YESNO = [("✅ Yes", "yes", 1), ("❌ No", "no", 2)]


YN = [("no", "No"), ("yes", "Yes")]


def bg_is_transparent(path):
    """True if the aircraft image is a clean cutout on a transparent background,
    False if it has an opaque/scenery background, None if unknown. Rim-based —
    mirrors the site's detection: sample the border ring and see if the edges are
    mostly transparent."""
    if not path:
        return None
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA")
        w, h = im.size
        px = im.load()
        step = max(1, min(w, h) // 120)
        transp = total = 0
        for x in range(0, w, step):
            for y in (0, h - 1):
                total += 1
                transp += px[x, y][3] < 30
        for y in range(0, h, step):
            for x in (0, w - 1):
                total += 1
                transp += px[x, y][3] < 30
        return total > 0 and transp / total > 0.6
    except Exception as e:
        log.warning("bg detect: %s", e)
        return None


def detect_bg_defaults(entry):
    """Owner spec: transparent bg → ground shadow default NO; opaque bg → YES.
    Returns (ground_default, bg_label)."""
    t = bg_is_transparent(map_path(entry.get("image_path")))
    if t is True:
        return "no", "🫥 透明背景 transparent — 已默认不加地面阴影"
    if t is False:
        return "yes", "🖼 非透明背景 has a background — 已默认加地面阴影"
    return "no", "背景未知 (unknown) — 默认不加地面阴影"


def combo_open_card(user, entry, reg):
    """The combined opening card: fictional? + ground? + drop? on ONE card, with
    the registration in the sub-title (type to correct). The ground-shadow default
    is auto-set from background detection. Falls back to sequential cards if the
    multi-select card can't be sent."""
    d = (SESS.get(user) or {}).get("data") or {}
    gd = d.get("ground_default", "no")
    bg_label = d.get("bg_label", "")
    ok = send_multi_card(
        user,
        f"Set up #{entry['id']}",
        f"Reg: {reg or '(none detected)'} — if wrong, TYPE the correct one, then "
        f"Confirm. Airline: {entry.get('airline_name','?')}\n{bg_label}",
        [("fictional", "Is this livery fictional?",
          [("no", "No — real airline"), ("yes", "Yes — fictional")], "no"),
         ("ground", "Add ground shadow?", YN, gd),
         ("drop", "Add drop shadow?", YN, "yes")],
        submit_key="combo_submit")
    return ok


def fcombo_open_card(user, entry, reg):
    """Combined FICTIONAL opening card (same design as the real one): ground? +
    drop? on ONE card, registration typed in. Airline + type are collected after
    (they're free text). Falls back to sequential cards if unavailable."""
    d = (SESS.get(user) or {}).get("data") or {}
    gd = d.get("ground_default", "no")
    bg_label = d.get("bg_label", "")
    return send_multi_card(
        user,
        f"Set up fictional #{entry['id']}",
        f"Reg: {reg or '(none)'} — TYPE the registration for this fictional "
        f"livery, then set the shadows and Confirm. (Airline & type asked next.)\n{bg_label}",
        [("ground", "Add ground shadow?", YN, gd),
         ("drop", "Add drop shadow?", YN, "yes")],
        submit_key="fcombo_submit")


def apply_fcombo(user, ans):
    """Apply the fictional combined card's answers (ground/drop), then ask airline."""
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    d["ground_shadow"] = ans.get("ground") == "yes"
    d["drop_shadow"] = ans.get("drop") == "yes"
    s["state"] = S_FAIRLINE
    save_sessions()
    return ask_card(user, f"Airline: {d.get('airline') or '(none)'}",
                    "Type the airline name for this fictional livery, or tap ✅ to "
                    "keep it as-is.", [("✅ Confirm airline", "yes", 1)])


def _send_entry_image(user, entry, followup, card=None, multi=False):
    """Send the original image, a caption, and optionally a tappable card (all in
    one thread so order is guaranteed). card=(title,desc,buttons) for a yes/no
    card; multi=True sends the combined opening card instead."""
    p = map_path(entry.get("image_path"))
    def _send():
        try:
            if p:
                send_media(user, p)
            else:
                send_text(user, "(original image not on server yet)")
            send_text(user, followup)
            if multi:
                if not combo_open_card(user, entry, entry.get("registration") or ""):
                    # multi-select card unavailable → sequential cards from the top
                    SESS[user]["state"] = S_FICTCHECK
                    save_sessions()
                    send_card(user, "Is this livery fictional?",
                              "Tap or type yes / no.", YESNO)
            elif card:
                send_card(user, card[0], card[1], card[2])
        except Exception as e:
            log.warning("entry image send: %s", e)
    bg(_send)


def start_article(user):
    entry = next_queued_entry()
    if not entry:
        return "队列为空 — no Queueing entry with a finished article. /articles 上查看。"
    gd, bg_label = detect_bg_defaults(entry)
    SESS[user] = {"state": S_COMBO,
                  "data": {"entry": entry, "reg": entry.get("registration") or "",
                           "ground_default": gd, "bg_label": bg_label,
                           "snapshot": snapshot_entry(entry["id"])}}
    _send_entry_image(user, entry,
        f"✈ #{entry['id']} {entry.get('airline_name','')} "
        f"{entry.get('registration','')} · credit: {entry.get('source_credit') or 'none'}",
        multi=True)
    return ""  # everything arrives via active sends


def start_fictional(user):
    entry = next_fictional_entry()
    if not entry:
        return "Fictional queue is empty (or images not on the server yet)."
    gd, bg_label = detect_bg_defaults(entry)
    SESS[user] = {"state": S_FCOMBO,
                  "data": {"entry": entry, "fictional": True,
                           "reg": entry.get("registration") or "",
                           "airline": entry.get("airline_name") or "",
                           "ground_default": gd, "bg_label": bg_label,
                           "snapshot": snapshot_entry(entry["id"])}}
    def _send():
        try:
            p = map_path(entry.get("image_path"))
            send_media(user, p) if p else send_text(user, "(original image not on server yet)")
            send_text(user, f"🎭 FICTIONAL #{entry['id']} {entry.get('airline_name','') or '(no name)'} "
                            f"{entry.get('registration','')} · credit: {entry.get('source_credit') or 'none'}")
            if not fcombo_open_card(user, entry, entry.get("registration") or ""):
                SESS[user]["state"] = S_FGROUND  # fall back to sequential cards
                save_sessions()
                send_card(user, "Add ground shadow?", "Tap or type yes / no.", YESNO_BTN)
        except Exception as e:
            log.warning("start_fictional send: %s", e)
    bg(_send)
    return ""


def leg_fictional_synth(user, d):
    """DeepSeek invents the data (site's fictional mode) — no planespotters,
    no article. Brew = 10 POSTERS only (fictional skips datasheets)."""
    entry = d["entry"]
    dsjson = None
    try:
        r = requests.post(f"{SITE}/api/synthesize",
                          json={"registration": d.get("reg", ""),
                                "airline": d.get("airline", ""),
                                "type_en": d.get("actype", ""),
                                "fictional": True},
                          timeout=200)
        if r.ok and "error" not in (r.json() or {}):
            dsjson = r.json()
            send_text(user, "🧪 DeepSeek fictional data compiled")
        else:
            send_text(user, f"⚠️ DeepSeek failed: {(r.json() or {}).get('error','?')[:120]}")
    except Exception as e:
        log.warning("fictional synth: %s", e)
        try:
            send_text(user, "⚠️ DeepSeek request failed.")
        except Exception:
            pass
    job = {"entry_id": entry["id"], "reg": d.get("reg", ""),
           "airline": d.get("airline", ""), "fictional": True,
           "poster_only": True, "expected_images": 10,
           "ground_shadow": d.get("ground_shadow"),
           "drop_shadow": d.get("drop_shadow"),
           "source_credit": entry.get("source_credit"),
           "license": entry.get("license"),
           "deepseek": dsjson, "created": time.time()}
    json.dump(job, open(os.path.join(BREW_DIR, f"{entry['id']}.json"), "w"),
              ensure_ascii=False, indent=1)
    send_text(user, "🎨 Brewing your infographics… (fictional mode: 10 posters, "
                    "no datasheets, no article) — gallery link arrives here "
                    "when the render finishes.")
    # hold the 'mark handled?' card until the render is done (brew_done fires it),
    # with a fallback timer so a stuck render never swallows the card.
    SESS[user]["state"] = S_FDONE
    SESS[user]["data"]["await_brew"] = entry["id"]
    SESS[user]["data"].pop("finish_card_sent", None)
    save_sessions()
    bg(finish_card_fallback, user, entry["id"])


# ------------------------------------------------- brew gallery + numbers
SESS_FILE = "/root/wecom-bot/sessions.json"
LAST_BREW = {"id": None}


def save_sessions():
    try:
        json.dump({"sess": SESS, "last_brew": LAST_BREW},
                  open(SESS_FILE, "w"), ensure_ascii=False)
    except Exception as e:
        log.warning("session save: %s", e)


def load_sessions():
    try:
        d = json.load(open(SESS_FILE))
        SESS.update(d.get("sess", {}))
        LAST_BREW.update(d.get("last_brew", {}))
        # A restart kills the daemon thread behind any "processing" session but
        # leaves the persisted state — that used to strand the owner on
        # "Still processing…". Recover such sessions to idle on startup.
        recovered = 0
        for u, v in list(SESS.items()):
            if (v or {}).get("state") == "processing":
                SESS[u] = {"state": S_IDLE, "data": {}}
                recovered += 1
        if recovered:
            log.info("recovered %s orphaned 'processing' session(s) -> idle", recovered)
            save_sessions()
        log.info("sessions restored: %s users, last_brew=%s",
                 len(SESS), LAST_BREW.get("id"))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("session load: %s", e)

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9, "十": 10}


def parse_style_numbers(text):
    """'3', '1 2 3', 'one two three', '三' … → sorted unique style numbers."""
    t = text.lower()
    nums = {int(x) for x in re.findall(r"\d+", t)}
    for w, n in _NUMWORDS.items():
        if re.search(rf"(?<![a-z]){w}(?![a-z])" if w.isascii() else w, t):
            nums.add(n)
    return sorted(n for n in nums if 1 <= n <= 20)


def brew_manifest(bid):
    p = f"/root/infographic-maker/data/brews/{bid}/manifest.json"
    try:
        return json.load(open(p))
    except Exception:
        return None


def leg_send_style(user, bid, nums):
    m = brew_manifest(bid)
    if not m or not m.get("files"):
        send_text(user, "No rendered images found for this brew yet.")
        return
    for n in nums:
        batch = [x for x in m["files"] if x.get("num") == n]
        if not batch:
            send_text(user, f"Style {n}: not found (1–{max(x.get('num',0) for x in m['files'])}).")
            continue
        # strict order: poster first, then datasheet
        batch.sort(key=lambda x: x["seq"])
        send_text(user, f"📦 Style {n} — {batch[0]['style']} ({len(batch)} image(s)), full resolution:")
        for x in batch:
            try:
                send_media(user, f"/root/infographic-maker/data/brews/{bid}/{x['file']}")
            except Exception as e:
                log.warning("style send: %s", e)
                send_text(user, f"⚠️ failed to send {x['file'][:40]}: {str(e)[:80]}")
    send_text(user, "Send another number anytime, or 'article' / 'fa' for the next entry.")


YESNO_BTN = [("✅ Yes", "yes", 1), ("❌ No", "no", 2)]


def ask_card(user, title, desc, buttons=None):
    """Send a tappable card and return '' (the answer arrives via the button)."""
    bg(send_card, user, title, desc, buttons or YESNO_BTN)
    return ""


def go_airframe(user, d):
    """Shared transition into the airframe-data step (auto-fetch or manual paste)."""
    SESS[user]["state"] = S_RAW
    reg = d.get("reg", "")
    if AF_AUTOFETCH:
        bg(leg_autofetch_airframe, user, d, reg)
        return (f"⏳ Fetching the airframe data from airfleets.net automatically "
                f"(~1 min)…\n🔗 https://www.planespotters.net/search?q={reg}\n\n"
                "If it comes up empty I'll ask you to paste it — or paste now.")
    return (f"🔗 https://www.planespotters.net/search?q={reg}\n\n"
            "Open the airframe page, copy the info text, and paste it back here.")


def apply_combo(user, ans):
    """Apply the combined opening card's answers (fictional/ground/drop)."""
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    e = d.get("entry") or {}
    if ans.get("fictional") == "yes":
        if e.get("id"):
            mark_fictional(e["id"])
        SESS[user] = {"state": S_IDLE, "data": {}}
        return ("🎭 Marked FICTIONAL — article discarded, entry moved to the "
                "fictional queue (use 'fa'). Send 'article' for the next real one.")
    d["ground_shadow"] = ans.get("ground") == "yes"
    d["drop_shadow"] = ans.get("drop") == "yes"
    return go_airframe(user, d)


def handle(user, text):
    s = SESS.setdefault(user, {"state": S_IDLE, "data": {}})
    st, d = s["state"], s["data"]
    t = text.strip()

    tl = t.lower()
    if tl == "ping":
        return "pong — IZhang Publisher V5.7 online"

    # ---- global commands (work in ANY state) ----
    if tl in ("status", "check status", "查询", "状态"):
        return status_text()
    if tl == "kill":
        return kill_job(user)
    if tl == "discard":
        # menu 'Discard this entry' — mark the current entry discarded (like skip→yes)
        if st == S_IDLE or not (d.get("entry") or {}).get("id"):
            return "No active entry to discard. Send 'article' or 'fa' first."
        d["resume_state"] = st
        s["state"] = S_CONFIRM_DISCARD
        return ask_card(user, "Discard this entry?",
                        "This marks the current entry Discarded. Tap or type yes / no.")
    if tl in ("published", "p", "mark published", "发布", "已发布"):
        # menu '✅ Mark published' — mark the current entry Published and move on,
        # from ANY state (reliable manual path, independent of the check-in card).
        e = d.get("entry") or {}
        if not e.get("id"):
            return "No active entry to mark. Send 'article' first, then 'published' when it's live."
        mark_entry(e["id"], "published")
        SESS[user] = {"state": S_IDLE, "data": {}}
        return "✔ Marked Published. Send 'article' (or ▶ Real) for the next one."
    # menu/card buttons and typed commands both start a fresh workflow
    if tl == "article":
        return start_article(user)
    if tl in ("fa", "fictional article"):
        return start_fictional(user)
    # style-number fetch works in the resting states too — the owner sends '2' at
    # the publish prompt expecting images, not a yes/no answer (regs 1–20 don't
    # exist, so a bare number here is unambiguous). Doesn't change state.
    if st in (S_IDLE, S_PUBLISHED, S_FDONE):
        nums = parse_style_numbers(t)
        if nums and LAST_BREW["id"]:
            bg(leg_send_style, user, LAST_BREW["id"], nums)
            return f"⏳ Sending style {', '.join(map(str, nums))}…"

    if st != S_IDLE and wants_skip(t):
        d["resume_state"] = st
        s["state"] = S_CONFIRM_DISCARD
        return ask_card(user, "Discard this entry?", "Tap or type yes / no.")

    if st == S_CONFIRM_DISCARD:
        if is_yes(t):
            e = d.get("entry") or {}
            if e.get("id"):
                mark_entry(e["id"], "discarded")
            SESS[user] = {"state": S_IDLE, "data": {}}
            return "Entry marked Discarded. Send 'article' for the next one."
        s["state"] = d.pop("resume_state", S_IDLE)
        return "OK, not discarded. " + reprompt(s["state"], d)

    if st == S_IDLE:
        # article / fa / status / kill are handled above as global commands
        nums = parse_style_numbers(t)
        if nums and LAST_BREW["id"]:
            bg(leg_send_style, user, LAST_BREW["id"], nums)
            return f"⏳ Sending style {', '.join(map(str, nums))}…"
        return ask_card(user, "What next?",
            "Tap a command, or type it. 数字取成品图 · ping 健康检查 · 底部菜单也可用。",
            [("▶ Real Article", "article", 1), ("🎭 Fictional", "fa", 1),
             ("📊 Status", "status", 1), ("✖ Kill", "kill", 2)])

    if st in (S_COMBO, S_FCOMBO):
        # the only free text expected on the combined card IS a registration
        # correction (the yes/no answers come from the card's submit). Accept it
        # leniently — real regs vary a lot (spaces, lengths, formats); don't gate
        # on a strict pattern or a valid correction gets bounced with the canned
        # reply. Strip an internal space on a single reg like the OCR path does.
        cand = t.strip()
        if cand.lower() not in ("yes", "no", "y", "n", "是", "否", "对", "不"):
            reg = cand if any(c in cand for c in ";/(),") else cand.replace(" ", "")
            if 1 <= len(reg) <= 20 and re.search(r"[A-Za-z0-9]", reg):
                d["reg"] = reg.upper()
                bg(fcombo_open_card if st == S_FCOMBO else combo_open_card,
                   user, d["entry"], d["reg"])
                return f"✅ 注册号已更新为 {d['reg']}，卡片已刷新——在卡片上选好后点 Confirm。"
        return ("这张卡片把问题合并了——在卡片上选好后点 Confirm。"
                "注册号不对就直接输入正确的（例如 B-2479）。")

    if st == S_FICTCHECK:
        if is_yes(t):
            e = d.get("entry") or {}
            if e.get("id"):
                mark_fictional(e["id"])
            SESS[user] = {"state": S_IDLE, "data": {}}
            return ("🎭 Marked FICTIONAL — article discarded, entry moved to the "
                    "fictional queue (use 'fa' to process it). Send 'article' for the next real one.")
        if is_no(t):
            s["state"] = S_GROUND
            return ask_card(user, "Add ground shadow?", "Tap or type yes / no.")
        return ask_card(user, "Is this livery fictional?", "Tap or type yes / no.")

    if st == S_GROUND:
        if is_yes(t) or is_no(t):
            d["ground_shadow"] = is_yes(t)
            s["state"] = S_DROP
            return ask_card(user, "Add drop shadow?", "Tap or type yes / no.")
        return ask_card(user, "Add ground shadow?", "Tap or type yes / no.")

    if st == S_DROP:
        if is_yes(t) or is_no(t):
            d["drop_shadow"] = is_yes(t)
            s["state"] = S_REG
            reg = d.get("reg") or "(none detected)"
            return ask_card(user, f"Registration: {reg}",
                            "Is the registration number correct? Tap to confirm, "
                            "or type the correct one.",
                            [("✅ Confirm registration number", "yes", 1)])
        return ask_card(user, "Add drop shadow?", "Tap or type yes / no.")

    if st == S_REG:
        if is_yes(t):
            return go_airframe(user, d)
        d["reg"] = t.strip()
        return ask_card(user, f"Registration: {d['reg']}",
                        "Updated. Tap to confirm, or type it again.",
                        [("✅ Confirm registration number", "yes", 1)])

    if st == S_RAW:
        reg = d.get("reg", "")
        low = t.strip().lower()
        # planespotters manual backup: reply 'planespotters' / 'ps' / 'manual'
        # to (re)surface the search link and switch to manual paste
        if low in ("planespotters", "ps", "manual", "手动"):
            return (f"🔗 https://www.planespotters.net/search?q={reg}\n\n"
                    "Open the airframe page, copy the info text, and paste it back here.")
        # retry airfleets auto-fetch on demand
        if low in ("airfleets", "af", "retry", "重试") and AF_AUTOFETCH:
            bg(leg_autofetch_airframe, user, d, reg)
            return "⏳ Retrying airfleets auto-fetch (~1 min)…"
        if len(t) < 40:
            return ("That looks too short to be the airframe text. Paste the full "
                    "airfleets/planespotters info, or reply 'planespotters' for the "
                    "manual search link, 'airfleets' to retry auto-fetch, or 'skip'.")
        s["state"] = "processing"
        bg(leg_deepseek_and_rest, user, d, t)
        return "⏳ Processing (DeepSeek → photo → Bing → article)…"

    if st == S_PUBLISHED:
        e = d.get("entry") or {}
        if is_yes(t):
            if e.get("id"):
                mark_entry(e["id"], "published")
            SESS[user] = {"state": S_IDLE, "data": {}}
            return "✔ Marked Published. Send 'article' for the next one."
        if is_no(t):
            SESS[user] = {"state": S_IDLE, "data": {}}
            return "Kept as Queueing. Send 'article' anytime."
        return ask_card(user, "Article published?", "Tap or type yes / no.")

    # ---------------- fictional (fa) flow ----------------
    if st == S_FGROUND:
        if is_yes(t) or is_no(t):
            d["ground_shadow"] = is_yes(t)
            s["state"] = S_FDROP
            return ask_card(user, "Add drop shadow?", "Tap or type yes / no.")
        return ask_card(user, "Add ground shadow?", "Tap or type yes / no.")

    if st == S_FDROP:
        if is_yes(t) or is_no(t):
            d["drop_shadow"] = is_yes(t)
            s["state"] = S_FREG
            return ask_card(user, f"Registration: {d.get('reg') or '(none)'}",
                            "Tap ✅ to confirm, or type the correct registration.",
                            [("✅ Confirm", "yes", 1)])
        return ask_card(user, "Add drop shadow?", "Tap or type yes / no.")

    if st == S_FREG:
        if not is_yes(t):
            d["reg"] = t.strip()
            return ask_card(user, f"Registration: {d['reg']}",
                            "Updated. Tap ✅ to confirm, or type it again.",
                            [("✅ Confirm", "yes", 1)])
        s["state"] = S_FAIRLINE
        return ask_card(user, f"Airline: {d.get('airline') or '(none)'}",
                        "Tap ✅ to confirm, or type the correct airline name.",
                        [("✅ Confirm", "yes", 1)])

    if st == S_FAIRLINE:
        if not is_yes(t):
            d["airline"] = t.strip()
            return ask_card(user, f"Airline: {d['airline']}",
                            "Updated. Tap ✅ to confirm, or type it again.",
                            [("✅ Confirm", "yes", 1)])
        s["state"] = S_FTYPE
        return ask_card(user, "Aircraft type?",
                        "Type it (e.g. Boeing 707-320B) so the poster matches the "
                        "drawing, or tap 'Let DeepSeek decide'.",
                        [("🤖 Let DeepSeek decide", "yes", 1)])

    if st == S_FTYPE:
        if not is_yes(t):
            d["actype"] = t.strip()
        s["state"] = "processing"
        bg(leg_fictional_synth, user, d)
        return "⏳ Compiling fictional data with DeepSeek…"

    if st == S_FDONE:
        e = d.get("entry") or {}
        if is_yes(t):
            if e.get("id"):
                mark_entry(e["id"], "published")
            SESS[user] = {"state": S_IDLE, "data": {}}
            return "✔ Fictional entry marked handled. 'fa' for the next one."
        if is_no(t):
            SESS[user] = {"state": S_IDLE, "data": {}}
            return "Kept in the fictional queue. Send 'fa' anytime."
        return ask_card(user, "Mark this fictional entry as handled/published?",
                        "Tap or type yes / no.")

    if st == "processing":
        return "⏳ Still processing the current entry — results arrive here as they finish."

    SESS[user] = {"state": S_IDLE, "data": {}}
    return "Reset. Send 'article' to start."


# ----------------------------------------------------------------- routes
@app.route("/wecom/callback", methods=["GET"])
def verify():
    a = request.args
    try:
        crypto.check_signature(a.get("msg_signature", ""), a.get("timestamp", ""),
                               a.get("nonce", ""), a.get("echostr", ""))
        return make_response(crypto.decrypt(a.get("echostr", "")), 200)
    except WeComCryptoError as e:
        log.warning("verify failed: %s", e)
        return make_response("forbidden", 403)


@app.route("/wecom/callback", methods=["POST"])
def receive():
    a = request.args
    try:
        root = ET.fromstring(request.data)
        enc = root.findtext("Encrypt", "")
        crypto.check_signature(a.get("msg_signature", ""), a.get("timestamp", ""),
                               a.get("nonce", ""), enc)
        inner = ET.fromstring(crypto.decrypt(enc))
    except (WeComCryptoError, ET.ParseError) as e:
        log.warning("receive rejected: %s", e)
        return make_response("forbidden", 403)

    user = inner.findtext("FromUserName", "")
    mtype = inner.findtext("MsgType", "")
    content = inner.findtext("Content", "") or ""
    msgid = inner.findtext("MsgId", "") or ""
    event = inner.findtext("Event", "") or ""
    ekey = inner.findtext("EventKey", "") or ""
    log.info("msg from=%s type=%s event=%s ekey=%s id=%s content=%r",
             user, mtype, event, ekey, msgid, content[:120])

    if ALLOWED and user.lower() not in ALLOWED:
        return make_response("", 200)

    def _passive(plain_xml):
        resp = make_response(crypto.build_reply(plain_xml), 200)
        resp.headers["Content-Type"] = "application/xml"
        return resp

    def _text_reply(txt):
        if not txt:
            return make_response("", 200)
        return _passive(f"<xml><ToUserName><![CDATA[{user}]]></ToUserName>"
                        f"<FromUserName><![CDATA[{CORP_ID}]]></FromUserName>"
                        f"<CreateTime>{int(time.time())}</CreateTime>"
                        f"<MsgType><![CDATA[text]]></MsgType>"
                        f"<Content><![CDATA[{txt}]]></Content></xml>")

    # ---- interactive buttons ----
    if mtype == "event":
        if event == "click":                       # persistent bottom-menu button
            cmd = CREATE_MENU_KEYS.get(ekey, "")
            return _text_reply(handle(user, cmd) if cmd else "")
        if event == "template_card_event":         # tappable in-message button
            # combined multi-select card submit → parse the dropdown answers
            sel = inner.find("SelectedItems")
            if sel is not None and (ekey in ("combo_submit", "fcombo_submit") or len(sel)):
                answers = {}
                for it in sel.findall("SelectedItem"):
                    qk = it.findtext("QuestionKey", "")
                    oid = ""
                    oids = it.find("OptionIds")
                    if oids is not None:
                        oid = oids.findtext("OptionId", "") or ""
                    if qk:
                        answers[qk] = oid
                reply = (apply_fcombo(user, answers) if ekey == "fcombo_submit"
                         or (SESS.get(user) or {}).get("state") == S_FCOMBO
                         else apply_combo(user, answers))
            else:                                  # single button: key is the answer text
                reply = handle(user, ekey)
            save_sessions()
            if reply:                              # deliver via active send; ack the button below
                bg(send_text, user, reply)
            return _passive(
                f"<xml><ToUserName><![CDATA[{user}]]></ToUserName>"
                f"<FromUserName><![CDATA[{CORP_ID}]]></FromUserName>"
                f"<CreateTime>{int(time.time())}</CreateTime>"
                f"<MsgType><![CDATA[update_button]]></MsgType>"
                f"<Button><ReplaceName><![CDATA[✓ done]]></ReplaceName></Button></xml>")
        return make_response("", 200)

    if mtype != "text":
        return make_response("", 200)
    # WeCom retries the same MsgId up to 3x if we're slow — process once
    now = time.time()
    for k in [k for k, v in SEEN_MSG.items() if now - v > 300]:
        SEEN_MSG.pop(k, None)
    if msgid and msgid in SEEN_MSG:
        return make_response("", 200)
    if msgid:
        SEEN_MSG[msgid] = now

    reply = handle(user, content)
    save_sessions()
    return _text_reply(reply)


@app.route("/internal/brew_done", methods=["POST"])
def internal_brew_done():
    b = request.get_json(force=True, silent=True) or {}
    if b.get("secret") != TOKEN:
        return make_response("forbidden", 403)
    bid = int(b.get("entry_id", 0))
    LAST_BREW["id"] = bid
    save_sessions()
    m = brew_manifest(bid) or {}
    styles = max((x.get("num", 0) for x in m.get("files", [])), default=0)
    def _notify():
        try:
            q = removebg_quota_line()
            send_text(OWNER, f"🎨 Infographics ready — {len(m.get('files', []))} images, "
                             f"{styles} styles:\n{PUBLIC_SITE}/brew/{bid}\n\n"
                             f"Reply a style number (1–{styles or 10}) and I'll send that "
                             f"set here in full resolution. Numbers work anytime."
                             + (f"\n\n{q}" if q else ""))
            # now that the gallery is here, ask the held publish/handled question
            maybe_send_finish_card(OWNER, bid)
        except Exception as e:
            log.warning("brew_done notify: %s", e)
    bg(_notify)
    return {"ok": True}


def send_finish_card_if_ready(user, force=False):
    """Send the 'published?' / 'handled?' card once BOTH gates are clear (the brew
    render + the reference-image e-mail) — or immediately when force=True (the
    fallback timer, so a stuck/slow brew can never swallow the card). Idempotent."""
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    if not force and (d.get("await_brew") or d.get("await_refs")):
        return                       # still waiting on the render or the e-mail
    if d.get("finish_card_sent"):
        return
    if s.get("state") == S_PUBLISHED:
        d["finish_card_sent"] = True
        d.pop("await_brew", None); d.pop("await_refs", None)
        save_sessions()
        send_card(user, "Article published?", "Tap or type yes / no.", YESNO_BTN)
    elif s.get("state") == S_FDONE:
        d["finish_card_sent"] = True
        d.pop("await_brew", None); d.pop("await_refs", None)
        save_sessions()
        send_card(user, "Mark this fictional entry as handled/published?",
                  "Tap or type yes / no.", YESNO_BTN)


def finish_card_fallback(user, entry_id, delay=200):
    """Guarantee the check-in card arrives: after `delay`s, if the flow is still
    parked on this entry and the card hasn't gone out (brew_done never fired,
    e-mail hung, etc.), force it. Runs in a background thread."""
    time.sleep(delay)
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    if d.get("finish_card_sent"):
        return
    if (d.get("entry") or {}).get("id") != entry_id:
        return                       # owner already moved on to another entry
    if s.get("state") in (S_PUBLISHED, S_FDONE):
        send_finish_card_if_ready(user, force=True)


def maybe_send_finish_card(user, bid):
    """Brew finished (or failed): clear the brew gate for that entry, then send
    the check-in card if the refs e-mail gate is also clear."""
    s = SESS.get(user) or {}
    d = s.get("data") or {}
    if d.get("await_brew") != bid:
        return
    d.pop("await_brew", None)
    save_sessions()
    send_finish_card_if_ready(user)


@app.route("/internal/notify", methods=["POST"])
def internal_notify():
    """Generic owner notification for trusted local automation (reset watcher
    on the Mac, cron jobs…). Guarded by the shared secret."""
    b = request.get_json(force=True, silent=True) or {}
    if b.get("secret") != TOKEN:
        return make_response("forbidden", 403)
    text = str(b.get("text", ""))[:1800]
    if text:
        bg(lambda: send_text(OWNER, text))
    return {"ok": True}


@app.route("/internal/brew_failed", methods=["POST"])
def internal_brew_failed():
    b = request.get_json(force=True, silent=True) or {}
    if b.get("secret") != TOKEN:
        return make_response("forbidden", 403)
    bid = b.get("entry_id")
    def _fail():
        send_text(OWNER, f"⚠️ Infographic render failed 3× for entry #{bid} — "
                         f"check brew_runner.log; you can still render manually "
                         f"at {PUBLIC_SITE}/aircraft?dbimg={bid}")
        maybe_send_finish_card(OWNER, int(bid) if bid is not None else -1)  # don't strand the flow
    bg(_fail)
    return {"ok": True}


@app.route("/wecom/health")
def health():
    return {"ok": True, "service": "izhang-publisher", "version": "5.7",
            "db": os.path.exists(DATA_DB), "bbid": os.path.exists(BBID)}


load_sessions()
bg(create_menu)  # (re)install the persistent quick-action menu on startup

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8642, threaded=True)
