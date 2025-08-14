#!/usr/bin/env python3
"""
Lunchdrop Future Menus Monitor — Always Slack + Robust Login
- Checks future Lunchdrop dates at: https://<city>.lunchdrop.com/app/YYYY-MM-DD
- Always sends a Slack message (new menus OR heartbeat).
- Re-goto each date after login so we snapshot the real page, not the sign-in screen.
"""

import os, hashlib, json, time
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import date, timedelta

# dotenv is optional (nice for local runs). On GitHub Actions we use env/secrets.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --------------------
# Config
# --------------------
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "14"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
LUNCHDROP_EMAIL = os.getenv("LUNCHDROP_EMAIL")
LUNCHDROP_PASSWORD = os.getenv("LUNCHDROP_PASSWORD")

# Optional tuning
CSS_CARD_SELECTORS = [s.strip() for s in os.getenv("CSS_CARD_SELECTORS", "").split(",") if s.strip()]
MIN_CARD_COUNT = int(os.getenv("MIN_CARD_COUNT", "1"))

# Runtime knobs
STATE_DIR = Path(os.getenv("STATE_DIR", ".ld_state"))
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "25000"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"  # default true for clearer Action logs

# Required env checks
missing = [k for k, v in {
    "BASE_URL": BASE_URL,
    "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL,
    "LUNCHDROP_EMAIL": LUNCHDROP_EMAIL,
    "LUNCHDROP_PASSWORD": LUNCHDROP_PASSWORD,
}.items() if not v]
if missing:
    raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

STATE_DIR.mkdir(parents=True, exist_ok=True)

# --------------------
# Helpers
# --------------------
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
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def url_for(d: date) -> str:
    return f"{BASE_URL}/{d.isoformat()}"

def state_path_for(url: str) -> Path:
    return STATE_DIR / (hashlib.md5(url.encode("utf-8")).hexdigest() + ".json")

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

# --------------------
# Auth & parsing
# --------------------
SIGNIN_SELECTORS = ["input[type=email]", "input[name=email]", "input[name=username]"]
PASSWORD_SELECTORS = ["input[type=password]", "input[name=password]"]
SUBMIT_SELECTORS = ["button:has-text('Sign in')", "button:has-text('Sign In')", "button[type=submit]"]

def ensure_logged_in(page):
    """Fill email/password and submit if a login form is present."""
    try:
        if any(page.locator(sel).count() > 0 for sel in PASSWORD_SELECTORS + SIGNIN_SELECTORS):
            print("🔐 Logging in…")
            # Email/username
            for sel in SIGNIN_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.fill(sel, LUNCHDROP_EMAIL)
                    break
            # Password
            for sel in PASSWORD_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.fill(sel, LUNCHDROP_PASSWORD)
                    break
            # Submit
            for sel in SUBMIT_SELECTORS:
                if page.locator(sel).count() > 0:
                    page.click(sel)
                    break
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            print("✅ Logged in (no errors thrown).")
        else:
            log("No login form detected; continuing.")
    except PlaywrightTimeoutError:
        print("⚠️ Login timeout; continuing anyway.")

def extract_snapshot_and_availability(page) -> Tuple[dict, bool, int]:
    """
    Returns: (snapshot_dict, available, count)
    available=True if 'cards' (or text hits) >= MIN_CARD_COUNT
    """
    # Preferred: caller supplies stable selectors that match 'cards'
    if CSS_CARD_SELECTORS:
        texts: List[str] = []
        total = 0
        for sel in CSS_CARD_SELECTORS:
            try:
                els = page.locator(sel).all()
                total += len(els)
                for el in els:
                    t = el.inner_text(timeout=2000)
                    if t:
                        texts.append(stable_text(t))
            except Exception:
                continue
        key = "\n".join(sorted(set(texts)))
        digest = content_hash(key)
        print(f"🧭 Selector scan: {total} card(s); digest={digest[:10]}…")
        return ({"mode": "selectors", "digest": digest}, total >= MIN_CARD_COUNT, total)

    # Fallback: search by text
    hits = 0
    try: hits += page.locator("text=Show Menu").count()
    except Exception: pass
    try: hits += page.locator("text=View Menu").count()
    except Exception: pass

    # Snapshot for digest
    main_text = None
    try:
        if page.locator("main").count() > 0:
            main_text = page.locator("main").inner_text(timeout=4000)
    except Exception:
        pass
    if not main_text:
        try:
            main_text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            main_text = ""
    key = stable_text(main_text)
    digest = content_hash(key)
    print(f"🧭 Text scan: {hits} hit(s); digest={digest[:10]}…")
    return ({"mode": "snapshot", "digest": digest}, hits >= MIN_CARD_COUNT, hits)

def check_date(browser, d: date) -> dict:
    url = url_for(d)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        print(f"🔎 Checking {d.isoformat()} → {url}")
        # First load (may land on sign-in)
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

        # Attempt login if needed
        ensure_logged_in(page)

        # **Revisit the date URL after login** to get the real page
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

        time.sleep(1.5)  # allow client-side render
        snap, avail, count = extract_snapshot_and_availability(page)
        snap["available"] = avail
        snap["count"] = count
        return {"url": url, "snap": snap}
    except PlaywrightTimeoutError:
        return {"url": url, "error": f"Timeout loading {url}"}
    except Exception as e:
        return {"url": url, "error": f"Error loading {url}: {e}"}
    finally:
        ctx.close()

# --------------------
# Main
# --------------------
def main():
    future_dates = [date.today() + timedelta(days=i) for i in range(1, LOOKAHEAD_DAYS + 1)]
    print(f"🚀 Scanning {LOOKAHEAD_DAYS} day(s): {future_dates[0].isoformat()} → {future_dates[-1].isoformat()}")

    newly_available = []
    errors = []

    with sync_playwright() as p:
        # Prefer installed Chrome (fast on GH runners); fallback to bundled engine
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        for d in future_dates:
            result = check_date(browser, d)
            url = result["url"]
            if "error" in result:
                print(f"⚠️  {result['error']}")
                errors.append(result["error"])
                continue

            snap = result["snap"]
            prev = load_state(url)
            prev_available = prev.get("available") if prev else None
            prev_digest = prev.get("digest") if prev else None
            save_state(url, snap)

            now_avail = snap.get("available", False)
            changed = (prev_digest != snap.get("digest"))

            if (prev_available in (None, False)) and now_avail:
                newly_available.append((d, url, snap.get("count", 0)))
                print(f"🎉 NEW: {d.isoformat()} became available (count={snap.get('count', 0)})")
            elif changed and now_avail:
                newly_available.append((d, url, snap.get("count", 0)))
                print(f"🔁 UPDATED: {d.isoformat()} content changed (count={snap.get('count', 0)})")

        browser.close()

    # --------------------
    # Always send a Slack message
    # --------------------
    if newly_available:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*🎉 New future Lunchdrop dates available:*"}}]
        for d, url, count in newly_available:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• *{d.isoformat()}* — <{url}|view> ({count} menus)"}})
        notify_slack("New future Lunchdrop dates available", blocks)
        print(f"📣 Notified Slack: {len(newly_available)} date(s)")
    else:
        # Heartbeat so you know it ran
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*✅ Lunchdrop monitor ran — no new future menus to report.*"}}]
        notify_slack("Lunchdrop monitor heartbeat — no new menus", blocks)
        print("📣 Sent heartbeat to Slack.")

    # Log any non-fatal warnings
    for e in errors:
        print(f"[warn] {e}")

if __name__ == "__main__":
    main()
