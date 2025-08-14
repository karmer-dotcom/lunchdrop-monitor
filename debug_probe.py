#!/usr/bin/env python3
"""
Lunchdrop debug probe
- Logs in
- Visits one date (BASE_URL/YYYY-MM-DD)
- Saves screenshot + HTML
- Sends a simple Slack ping with basic counts
"""

import os, time, json
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
LUNCHDROP_EMAIL = os.getenv("LUNCHDROP_EMAIL")
LUNCHDROP_PASSWORD = os.getenv("LUNCHDROP_PASSWORD")
PROBE_OFFSET_DAYS = int(os.getenv("PROBE_OFFSET_DAYS", "0"))  # 0=today, 1=tomorrow, etc.
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "25000"))

assert BASE_URL and SLACK_WEBHOOK_URL and LUNCHDROP_EMAIL and LUNCHDROP_PASSWORD, "Missing env vars"

out = Path("probe_artifacts"); out.mkdir(exist_ok=True)
d = date.today() + timedelta(days=PROBE_OFFSET_DAYS)
url = f"{BASE_URL}/{d.isoformat()}"

def notify_slack(text: str):
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[warn] Slack failed: {e}")

def ensure_logged_in(page):
    try:
        # if we see a password field, do login
        if page.locator("input[type=password], input[name=password]").count() > 0:
            print("🔐 Logging in…")
            if page.locator("input[type=email], input[name=email], input[name=username]").count() > 0:
                page.fill("input[type=email], input[name=email], input[name=username]", LUNCHDROP_EMAIL)
            page.fill("input[type=password], input[name=password]", LUNCHDROP_PASSWORD)
            # any submit-ish button
            if page.locator("button:has-text('Sign in'), button:has-text('Sign In'), button[type=submit]").count() > 0:
                page.click("button:has-text('Sign in'), button:has-text('Sign In'), button[type=submit]")
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            print("✅ Login attempted.")
    except PlaywrightTimeoutError:
        print("⚠️ Login timeout; continuing")

def main():
    print(f"🚀 Probe for {d.isoformat()} → {url}")
    with sync_playwright() as p:
        try:
            try:
                browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
            except Exception:
                browser = p.chromium.launch(headless=HEADLESS)
            ctx = browser.new_context()
            page = ctx.new_page()

            # first visit (may land on signin)
            page.goto(url, timeout=TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

            ensure_logged_in(page)

            # revisit the date page post-auth
            page.goto(url, timeout=TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            time.sleep(1.5)

            # quick heuristics: count obvious text hits
            hits = 0
            try: hits += page.locator("text=Show Menu").count()
            except Exception: pass
            try: hits += page.locator("text=View Menu").count()
            except Exception: pass

            # save artifacts
            png_path = out / f"screenshot-{d.isoformat()}.png"
            html_path = out / f"page-{d.isoformat()}.html"
            page.screenshot(path=str(png_path), full_page=True)
            html = page.content()
            html_path.write_text(html, encoding="utf-8")

            print(f"📸 Saved {png_path} and {html_path}")
            print(f"🧭 Text hits (Show/View Menu): {hits}")

            notify_slack(f"✅ Lunchdrop probe ran for {d.isoformat()} — text hits: {hits}")

            ctx.close()
            browser.close()
        except Exception as e:
            print(f"❌ Probe error: {e}")
            notify_slack(f"❌ Lunchdrop probe error: {e}")

if __name__ == "__main__":
    main()
