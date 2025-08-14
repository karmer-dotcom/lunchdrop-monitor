#!/usr/bin/env python3
"""
Lunchdrop Future Menus Monitor — v4 (message-based detection, always Slack)
- Detects menus by checking for the absence of the "no menus yet" message.
- Always sends a Slack message (new menus OR heartbeat).
"""

import os, hashlib, json, time
from pathlib import Path
from typing import Optional, Tuple
from datetime import date, timedelta

# Banner: show version + commit SHA if available
SCRIPT_VERSION = "v4"
GITHUB_SHA = os.getenv("GITHUB_SHA", "")[:7]
print(f"🚀 Lunchdrop monitor {SCRIPT_VERSION}  commit={GITHUB_SHA or 'local'}")

# Optional dotenv for local runs; in Actions we use env/secrets
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

# Message shown when no menus exist yet (case-insensitive match)
NO_MENU_MESSAGE = os.getenv(
    "NO_MENU_MESSAGE",
    "The restaurants for this day will be scheduled shortly."
).strip()

STATE_DIR = Path(os.getenv("STATE_DIR", ".ld_state"))
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "25000"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"

missing = [k for k, v in {
    "BASE_URL": BASE_URL, "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL,
    "LUNCHDROP_EMAIL": LUNCHDROP_EMAIL, "LUNCHDROP_PASSWORD": LUNCHDROP_PASSWORD,
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
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    r.raise_for_status()

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
# Auth & detection
# --------------------
SIGNIN_SELECTORS = ["input[type=email]", "input[name=email]", "input[name=username]"]
PASSWORD_SELECTORS = ["input[type=password]", "input[name=password]"]
SUBMIT_SELECTORS = ["button:has-text('Sign in')", "button:has-text('Sign In')", "button[type=submit]"]

def ensure_logged_in(page):
    """Fill email/password and submit if a login form is present."""
    try:
        if any(page.locator(sel).count() > 0 for sel in PASSWORD_SELECTORS + SIGNIN_SELECTORS):
            print("🔐 Logging in…")
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
            print("✅ Logged in (attempt complete).")
        else:
            log("No login form detected; continuing.")
    except PlaywrightTimeoutError:
        print("⚠️ Login timeout; continuing anyway.")

def detect_availability_and_digest(page) -> Tuple[bool, str]:
    """
    Heuristic: if NO_MENU_MESSAGE appears (case-insensitive) => no menus.
               else => menus are available.
    Returns: (available, digest_of_page_text)
    """
    # Prefer main; fall back to body
    txt = ""
    try:
        if page.locator("main").count() > 0:
            txt = page.locator("main").inner_text(timeout=4000)
    except Exception:
        pass
    if not txt:
        try:
            txt = page.locator("body").inner_text(timeout=4000)
        except Exception:
            txt = ""

    norm = stable_text(txt).lower()
    available = NO_MENU_MESSAGE.lower() not in norm
    digest = content_hash(norm)
    print(f"🔎 Message check: available={available}  digest={digest[:10]}…")
    return available, digest

def check_date(browser, d: date) -> dict:
    url = url_for(d)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        print(f"📅 Checking {d.isoformat()} → {url}")
        # First load (may hit sign-in)
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        ensure_logged_in(page)

        # Revisit the date URL post-auth
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        time.sleep(1.5)  # let client JS render

        available, digest = detect_availability_and_digest(page)
        return {"url": url, "available": available, "digest": digest}
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
    print(f"📆 Window: {future_dates[0].isoformat()} → {future_dates[-1].isoformat()}  (days={LOOKAHEAD_DAYS})")
    print(f"ℹ️ Using no-menu message: “{NO_MENU_MESSAGE}”")

    newly_available = []
    errors = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        for d in future_dates:
            r = check_date(browser, d)
            url = r["url"]
            if "error" in r:
                print(f"⚠️ {r['error']}")
                errors.append(r["error"])
                continue

            available_now = r["available"]
            digest_now = r["digest"]

            prev = load_state(url) or {}
            prev_available = prev.get("available")
            prev_digest = prev.get("digest")

            save_state(url, {"available": available_now, "digest": digest_now})

            # Alert when a day first becomes available or its content changes while available
            if (prev_available in (None, False)) and available_now:
                newly_available.append((d, url))
                print(f"🎉 NEW: {d.isoformat()} became available")
            elif available_now and prev_digest and prev_digest != digest_now:
                newly_available.append((d, url))
                print(f"🔁 UPDATED: {d.isoformat()} content changed")

        browser.close()

    # Always send Slack
    if newly_available:
        blocks = [{"type":"section","text":{"type":"mrkdwn","text":"*🎉 New future Lunchdrop dates available:*"}}]
        for d, url in newly_available:
            blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"• *{d.isoformat()}* — <{url}|view>"}})
        notify_slack("New future Lunchdrop dates available", blocks)
        print(f"📣 Notified Slack: {len(newly_available)} date(s)")
    else:
        blocks = [
            {"type":"section","text":{"type":"mrkdwn","text":"*✅ Lunchdrop monitor ran — no new future menus to report.*"}},
            {"type":"context","elements":[{"type":"mrkdwn","text":f"Window: {future_dates[0]} → {future_dates[-1]}"}]}
        ]
        notify_slack("Lunchdrop monitor heartbeat — no new menus", blocks)
        print("📣 Sent heartbeat to Slack.")

    for e in errors:
        print(f"[warn] {e}")

if __name__ == "__main__":
    main()
