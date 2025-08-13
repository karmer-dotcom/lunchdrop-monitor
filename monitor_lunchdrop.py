#!/usr/bin/env python3
"""
Lunchdrop Future Menus Monitor (password login + Slack)
Runs in GitHub Actions on a schedule. Checks a rolling window of FUTURE dates like:
  https://<city>.lunchdrop.com/app/YYYY-MM-DD
and alerts Slack when a date that used to be empty now shows menus.

Env vars (set as GitHub Secrets or Workflow env):
BASE_URL=https://austin.lunchdrop.com/app
LOOKAHEAD_DAYS=14
SLACK_WEBHOOK_URL=...
LUNCHDROP_EMAIL=...
LUNCHDROP_PASSWORD=...
HEADLESS=true
# Optional:
# CSS_CARD_SELECTORS=.card:has-text("Show Menu"), .restaurant-card
# MIN_CARD_COUNT=1
# VERBOSE=true
"""
import os, hashlib, json, time
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "14"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
LUNCHDROP_EMAIL = os.getenv("LUNCHDROP_EMAIL")
LUNCHDROP_PASSWORD = os.getenv("LUNCHDROP_PASSWORD")
CSS_CARD_SELECTORS = [s.strip() for s in os.getenv("CSS_CARD_SELECTORS", "").split(",") if s.strip()]
MIN_CARD_COUNT = int(os.getenv("MIN_CARD_COUNT", "1"))

STATE_DIR = Path(os.getenv("STATE_DIR", ".ld_state"))
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "25000"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"

if not BASE_URL:
    raise SystemExit("Please set BASE_URL (e.g. https://austin.lunchdrop.com/app)")
if not SLACK_WEBHOOK_URL:
    raise SystemExit("Please set SLACK_WEBHOOK_URL")
if not (LUNCHDROP_EMAIL and LUNCHDROP_PASSWORD):
    raise SystemExit("Please set LUNCHDROP_EMAIL and LUNCHDROP_PASSWORD")

STATE_DIR.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    if VERBOSE:
        print(msg)

def notify_slack(text: str, blocks: Optional[list] = None):
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()

def stable_text(s: str) -> str:
    return " ".join(s.split())

def content_hash(content: str) -> str:
    import hashlib as _h
    return _h.sha256(content.encode("utf-8")).hexdigest()

def url_for(d: date) -> str:
    return f"{BASE_URL}/{d.isoformat()}"

def state_path_for(url: str) -> Path:
    import hashlib as _h
    return STATE_DIR / (_h.md5(url.encode("utf-8")).hexdigest() + ".json")

def save_state(url: str, data: dict):
    with open(state_path_for(url), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_state(url: str) -> Optional[dict]:
    p = state_path_for(url)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

SIGNIN_SELECTORS = ["input[type=email]", "input[name=email]", "input[name=username]"]
PASSWORD_SELECTORS = ["input[type=password]", "input[name=password]"]
SUBMIT_SELECTORS = ["button:has-text('Sign in')", "button:has-text('Sign In')", "button[type=submit]"]

def ensure_logged_in(page):
    try:
        if any(page.locator(sel).count() > 0 for sel in PASSWORD_SELECTORS + SIGNIN_SELECTORS):
            log("Login form detected; sign-in…")
            for sel in SIGNIN_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.fill(sel, LUNCHDROP_EMAIL); break
            for sel in PASSWORD_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.fill(sel, LUNCHDROP_PASSWORD); break
            for sel in SUBMIT_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.click(sel); break
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PlaywrightTimeoutError:
        log("Timed out during login.")

def extract_snapshot_and_availability(page) -> Tuple[dict, bool, int]:
    if CSS_CARD_SELECTORS:
        texts: List[str] = []; total = 0
        for sel in CSS_CARD_SELECTORS:
            try:
                els = page.locator(sel).all(); total += len(els)
                for el in els:
                    t = el.inner_text(timeout=2000)
                    if t: texts.append(stable_text(t))
            except Exception:
                continue
        key = "\n".join(sorted(set(texts)))
        digest = content_hash(key)
        if VERBOSE: print(f"[debug] selectors cards={total} digest={digest[:10]}…")
        return ({"mode": "selectors", "digest": digest}, total >= MIN_CARD_COUNT, total)

    hits = 0
    try: hits += page.locator("text=Show Menu").count()
    except Exception: pass
    try: hits += page.locator("text=View Menu").count()
    except Exception: pass

    main_text = None
    try:
        if page.locator("main").count() > 0:
            main_text = page.locator("main").inner_text(timeout=4000)
    except Exception:
        pass
    if not main_text:
        try: main_text = page.locator("body").inner_text(timeout=4000)
        except Exception: main_text = ""
    key = stable_text(main_text); digest = content_hash(key)
    if VERBOSE: print(f"[debug] text_hits={hits} digest={digest[:10]}…")
    return ({"mode": "snapshot", "digest": digest}, hits >= MIN_CARD_COUNT, hits)

def check_date(browser, d: date) -> dict:
    url = url_for(d)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        ensure_logged_in(page)
        time.sleep(1.2)
        snap, avail, count = extract_snapshot_and_availability(page)
        snap["available"] = avail; snap["count"] = count
        return {"url": url, "snap": snap}
    except PlaywrightTimeoutError:
        return {"url": url, "error": f"Timeout loading {url}"}
    except Exception as e:
        return {"url": url, "error": f"Error loading {url}: {e}"}
    finally:
        ctx.close()

def main():
    future_dates = [date.today() + timedelta(days=i) for i in range(1, LOOKAHEAD_DAYS + 1)]
    newly_available = []; errors = []

    with sync_playwright() as p:
        # Use preinstalled Chrome on GitHub runner; fallback to bundled if available
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        for d in future_dates:
            print(f"Checking {d.isoformat()} …")
            result = check_date(browser, d)
            url = result["url"]
            if "error" in result:
                errors.append(result["error"]); continue

            snap = result["snap"]; prev = load_state(url)
            prev_available = prev.get("available") if prev else None
            prev_digest = prev.get("digest") if prev else None
            save_state(url, snap)

            now_avail = snap.get("available", False)
            changed = (prev_digest != snap.get("digest"))

            if (prev_available in (None, False)) and now_avail:
                newly_available.append((d, url, snap.get("count", 0)))
            elif changed and now_avail:
                newly_available.append((d, url, snap.get("count", 0)))

        browser.close()

    if newly_available:
        blocks = [{"type":"section","text":{"type":"mrkdwn","text":"*🎉 New future Lunchdrop dates available:*"}}]
        for d, url, count in newly_available:
            blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"• *{d.isoformat()}* — <{url}|view> ({count} menus)"}})
        blocks.append({"type":"divider"})
        notify_slack("New future Lunchdrop dates available", blocks)
        print(f"Notified Slack: {len(newly_available)} date(s)")
    else:
        print("No new future menus detected.")

    for e in errors:
        print(f"[warn] {e}")

if __name__ == "__main__":
    main()
