#!/usr/bin/env python3
"""
Lunchdrop Future Menus Monitor — v5
- Availability: message-based (absent => menus available).
- Names: heuristic extraction (buttons "Show/View Menu" -> nearest card -> heading).
- Always sends Slack (new menus OR heartbeat).
"""

import os, hashlib, json, time, re
from pathlib import Path
from typing import Optional, Tuple
from datetime import date, timedelta

# Banner: show version + commit SHA if available
SCRIPT_VERSION = "v5"
GITHUB_SHA = os.getenv("GITHUB_SHA", "")[:7]
print(f"🚀 Lunchdrop monitor {SCRIPT_VERSION}  commit={GITHUB_SHA or 'local'}")

# Optional dotenv for local runs
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

# Message shown when no menus exist yet (case-insensitive)
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

def detect_availability_and_digest(page) -> Tuple[bool, str, str]:
    """
    Heuristic: if NO_MENU_MESSAGE appears (case-insensitive) => no menus.
               else => menus are available.
    Returns: (available, digest_of_page_text, normalized_text)
    """
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

    norm = stable_text(txt)
    available = NO_MENU_MESSAGE.lower() not in norm.lower()
    digest = content_hash(norm.lower())
    print(f"🔎 Message check: available={available}  digest={digest[:10]}…")
    return available, digest, norm

def extract_restaurant_names(page) -> list[str]:
    """
    Robust attempts to pull restaurant names.
    Strategy A: find "Show/View Menu" buttons, walk up to nearest card-like container, prefer heading text.
    Strategy B: fallback — grab short headings on the page that don’t look like generic 'menu' text.
    """
    names = set()

    # Strategy A
    try:
        buttons = page.get_by_role("button", name=re.compile(r"(show|view)\s*menu", re.I)).all()
        for b in buttons:
            try:
                # nearest plausible card container
                container = b.locator(
                    "xpath=ancestor::*[self::article or self::section or contains(@class,'card') or contains(@class,'Card')][1]"
                )
                # prefer an explicit heading
                head = container.get_by_role("heading").first
                name = ""
                if head.count() > 0:
                    name = head.inner_text(timeout=2000).strip()
                else:
                    # fallback: first non-empty line of container text that isn't the button
                    txt = container.inner_text(timeout=2000)
                    for line in (l.strip() for l in txt.splitlines()):
                        if line and not re.search(r"(show|view)\s*menu", line, re.I):
                            name = line; break
                if name:
                    names.add(stable_text(name))
            except Exception:
                continue
    except Exception:
        pass

    # Strategy B (fallback)
    if not names:
        try:
            for h in page.get_by_role("heading").all():
                try:
                    t = h.inner_text(timeout=1500).strip()
                    if t and len(t) <= 60 and not re.search(r"\bmenu\b", t, re.I):
                        names.add(stable_text(t))
                except Exception:
                    continue
        except Exception:
            pass

    return sorted(n for n in names if n)

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

        available, digest, _norm = detect_availability_and_digest(page)
        names = []
        if available:
            try:
                names = extract_restaurant_names(page)
                print(f"🍽️  Found {len(names)} restaurant name(s): {', '.join(names[:6])}{'…' if len(names)>6 else ''}")
            except Exception as e:
                print(f"⚠️ name extraction failed: {e}")

        return {"url": url, "available": available, "digest": digest, "names": names}
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

            snap_now = {"available": r["available"], "digest": r["digest"], "names": r.get("names", [])}
            prev = load_state(url) or {}
            prev_available = prev.get("available")
            prev_digest = prev.get("digest")
            prev_names = set(prev.get("names", []))
            now_names = set(snap_now.get("names", []))

            save_state(url, snap_now)

            became_available = (prev_available in (None, False)) and snap_now["available"]
            names_added = sorted(now_names - prev_names)

            if became_available:
                newly_available.append((d, url, names_added or sorted(now_names)))
                print(f"🎉 NEW: {d.isoformat()} became available; names={', '.join(names_added or now_names)}")
            elif snap_now["available"] and prev_digest and prev_digest != snap_now["digest"]:
                newly_available.append((d, url, names_added))
                print(f"🔁 UPDATED: {d.isoformat()} content changed; +{len(names_added)} name(s)")

        browser.close()

    # Always send Slack
    if newly_available:
        blocks = [{"type":"section","text":{"type":"mrkdwn","text":"*🎉 New future Lunchdrop dates available:*"}}]
        for d, url, names_added in newly_available:
            line = f"• *{d.isoformat()}* — <{url}|view>"
            if names_added:
                shown = names_added[:6]
                extra = len(names_added) - len(shown)
                line += " — " + ", ".join(shown)
                if extra > 0:
                    line += f"  _(+{extra} more)_"
            blocks.append({"type":"section","text":{"type":"mrkdwn","text": line}})
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
