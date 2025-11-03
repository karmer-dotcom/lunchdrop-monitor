#!/usr/bin/env python3
"""
Lunchdrop Future Menus Monitor — v7.6
- Parse Inertia payload (#app[data-page]) for reliable availability + names
- Direct sign-in URL, persisted auth (storage_state) reused across dates
- Skips weekends (Mon–Fri only)
- SUMMARY_ONLY mode: Slack roll-up (one "click here to order" link at the top)
- Normal mode: diff detection + alerts; artifacts on auth failure or "no menus"
- NEW: SEND_HEARTBEAT env flag to suppress the no-change heartbeat
"""

import os, hashlib, json, re, string
from pathlib import Path
from typing import Optional, Tuple
from datetime import date, timedelta

# ----- Banner -----
SCRIPT_VERSION = "v7.6"
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

# ----- Config -----
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")            # e.g. https://austin.lunchdrop.com/app
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
LUNCHDROP_EMAIL = os.getenv("LUNCHDROP_EMAIL")
LUNCHDROP_PASSWORD = os.getenv("LUNCHDROP_PASSWORD")
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "14"))
SUMMARY_ONLY = os.getenv("SUMMARY_ONLY", "false").lower() == "true"
SEND_HEARTBEAT = os.getenv("SEND_HEARTBEAT", "false").lower() == "true"

# Sign-in URL: default guesses <root>/signin. Override via env if needed.
def infer_signin_url(base: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit
        sp = urlsplit(base)
        path = sp.path
        if path.endswith("/app"):
            path = path[:-4]
        root = urlunsplit((sp.scheme, sp.netloc, path.rstrip("/"), "", ""))
        return f"{root}/signin"
    except Exception:
        return base + "/signin"

SIGNIN_URL = os.getenv("SIGNIN_URL", infer_signin_url(BASE_URL)).rstrip("/")

# Runtime + paths
STATE_DIR = Path(os.getenv("STATE_DIR", ".ld_state"))
AUTH_DIR = Path(os.getenv("AUTH_DIR", ".auth"))
AUTH_STATE = AUTH_DIR / "state.json"
ART_DIR = Path(os.getenv("ART_DIR", "artifacts"))
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "25000"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"

missing = [k for k, v in {
    "BASE_URL": BASE_URL, "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL,
    "LUNCHDROP_EMAIL": LUNCHDROP_EMAIL, "LUNCHDROP_PASSWORD": LUNCHDROP_PASSWORD,
}.items() if not v]
if missing:
    raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

# Guard for sign-in URL
if not SIGNIN_URL.startswith("http"):
    raise SystemExit(f"SIGNIN_URL invalid: {SIGNIN_URL!r} — set SIGNIN_URL=https://austin.lunchdrop.com/signin")

for d in (STATE_DIR, AUTH_DIR, ART_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ----- Helpers -----
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

# ----- Safe selector utilities -----
SIGNIN_SELECTORS = ["input[type=email]", "input[name=email]", "input[name=username]"]
PASSWORD_SELECTORS = ["input[type=password]", "input[name=password]"]
SUBMIT_SELECTORS = [
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
    "button:has-text('Log in')",
    "button:has-text('Log In')",
    "button[type=submit]",
    "input[type=submit]",
]

# NEW: some sites split username and password across two steps
CONTINUE_SELECTORS = [
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "[data-testid='continue']",
    "[data-test='continue']",
]

def safe_has(page, sel: str) -> bool:
    try:
        return page.locator(sel).count() > 0
    except Exception:
        return False

def try_click_any(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                return True
        except Exception:
            continue
    return False

# ----- Auth (persist + reuse) -----
def ensure_logged_in_and_save_state(browser) -> None:
    """
    Go directly to SIGNIN_URL, handle username→continue→password flows if needed,
    then open BASE_URL to verify and save storage_state to AUTH_STATE.
    """
    ctx = browser.new_context()
    page = ctx.new_page()
    print(f"🔐 Auth at {SIGNIN_URL}")
    try:
        page.goto(SIGNIN_URL, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

        # Detect any sign-in UI
        if any(safe_has(page, s) for s in PASSWORD_SELECTORS + SIGNIN_SELECTORS):
            print("🧾 Login form detected; attempting login…")

            # Step 1: username/email
            filled_username = False
            for sel in SIGNIN_SELECTORS:
                if safe_has(page, sel):
                    page.fill(sel, LUNCHDROP_EMAIL)
                    filled_username = True
                    break

            # If we filled a username, try to advance with Continue/Next/Enter.
            if filled_username:
                advanced = try_click_any(page, CONTINUE_SELECTORS)
                if not advanced:
                    # Some pages advance on submit/enter even in step 1
                    advanced = try_click_any(page, SUBMIT_SELECTORS)
                if not advanced:
                    page.keyboard.press("Enter")

                # Give the page a beat to swap in password UI
                try:
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
                except Exception:
                    pass
                page.wait_for_timeout(800)  # small settle time

            # Step 2: password (may already be present on same page)
            password_present = any(safe_has(page, s) for s in PASSWORD_SELECTORS)
            if not password_present:
                # If it’s truly two-step, wait briefly for a password input to appear.
                try:
                    page.wait_for_selector(",".join(PASSWORD_SELECTORS), timeout=TIMEOUT_MS)
                    password_present = True
                except Exception:
                    password_present = any(safe_has(page, s) for s in PASSWORD_SELECTORS)

            if password_present:
                for sel in PASSWORD_SELECTORS:
                    if safe_has(page, sel):
                        page.fill(sel, LUNCHDROP_PASSWORD)
                        break

                # Final submit
                submitted = try_click_any(page, SUBMIT_SELECTORS)
                if not submitted:
                    # Some UIs reuse the Continue button for the final submit
                    submitted = try_click_any(page, CONTINUE_SELECTORS)
                if not submitted:
                    page.keyboard.press("Enter")

                try:
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            print("✅ Login submitted.")

        # Visit BASE_URL to ensure app view & a valid session
        page.goto(BASE_URL, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        page.wait_for_timeout(1000)

        # If we still see auth fields, capture artifacts and fail
        still_login = any(safe_has(page, s) for s in PASSWORD_SELECTORS + SIGNIN_SELECTORS)
        if still_login:
            page.screenshot(path=str(ART_DIR / "auth-failed-screen.png"), full_page=True)
            (ART_DIR / "auth-failed-page.html").write_text(page.content(), encoding="utf-8")
            raise RuntimeError("Login still showing fields after submit")

        ctx.storage_state(path=str(AUTH_STATE))
        print(f"🔒 Auth state saved to {AUTH_STATE}")
    finally:
        ctx.close()


# ----- Payload-based detection -----
def detect_availability_and_deliveries(page) -> tuple[bool, list[dict], str]:
    """
    Parse Lunchdrop Inertia payload for open deliveries.
    Returns:
      available: bool
      deliveries: list of dicts {name, url}
      digest: str
    """
    try:
        page.wait_for_selector('#app[data-page]', timeout=TIMEOUT_MS)
        raw = page.get_attribute('#app', 'data-page')
        if not raw:
            return False, [], content_hash("")
        data = json.loads(raw)

        deliveries = (
            data.get("props", {}).get("lunchDay", {}).get("deliveries")
            or ([data["props"]["delivery"]] if data.get("props", {}).get("delivery") else [])
            or []
        )

        open_deliveries = [
            d for d in deliveries
            if (d.get("isOpen") in (1, True)) and not d.get("isCancelled") and d.get("userCanOrder", True)
        ]

        info = []
        for d in open_deliveries:
            name = d.get("restaurantName") or (d.get("restaurant") or {}).get("name", "")
            url = d.get("url") or d.get("link") or ""
            if name:
                info.append({"name": name, "url": url})

        digest = content_hash(json.dumps(sorted([i["name"] for i in info]), sort_keys=True))
        return bool(open_deliveries), info, digest
    except Exception as e:
        print(f"⚠️ payload parse failed: {e}")
        return False, [], content_hash("")

# ----- Per-date check (reusing auth) -----
def check_date_with_auth(browser, d: date) -> dict:
    url = url_for(d)
    ctx = browser.new_context(storage_state=str(AUTH_STATE) if AUTH_STATE.exists() else None)
    page = ctx.new_page()
    try:
        print(f"📅 Checking {d.isoformat()} → {url}")
        page.goto(url, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        page.wait_for_timeout(1000)

        available, deliveries, digest = detect_availability_and_deliveries(page)
        names = [r["name"] for r in deliveries]

        if available:
            print(f"🍽️  Found {len(names)} restaurant name(s): {', '.join(names)}")
        else:
            # Save artifacts so we can see what page looked like
            png_path = ART_DIR / f"{d.isoformat()}-screen.png"
            html_path = ART_DIR / f"{d.isoformat()}-page.html"
            try:
                page.screenshot(path=str(png_path), full_page=True)
                html_path.write_text(page.content(), encoding="utf-8")
                print(f"🗂️  Saved artifacts: {png_path.name}, {html_path.name}")
            except Exception as e:
                print(f"⚠️ artifact save failed: {e}")

        return {
            "url": url,
            "available": available,
            "digest": digest,
            "names": names,
            "deliveries": deliveries
        }
    except PlaywrightTimeoutError:
        return {"url": url, "error": f"Timeout loading {url}"}
    except Exception as e:
        return {"url": url, "error": f"Error loading {url}: {e}"}
    finally:
        ctx.close()

# ----- Main -----
def main():
    # Only include weekdays (Mon=0 .. Fri=4)
    weekdays = [
        date.today() + timedelta(days=i)
        for i in range(1, LOOKAHEAD_DAYS + 1)
        if (date.today() + timedelta(days=i)).weekday() < 5
    ]
    if not weekdays:
        print("ℹ️ No weekdays in the requested lookahead window.")
        return

    print(f"📆 Window (weekdays only): {weekdays[0].isoformat()} → {weekdays[-1].isoformat()}  (days={len(weekdays)})")
    print(f"ℹ️ SIGNIN_URL={SIGNIN_URL}")

    newly_available = []
    errors = []

    with sync_playwright() as p:
        # Launch browser
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        # 1) Login once and save storage state
        try:
            ensure_logged_in_and_save_state(browser)
        except Exception as e:
            print(f"❌ Login failed: {e}")
            notify_slack(f"❌ Lunchdrop monitor login failed: {e}")
            browser.close()
            return

        # --- SUMMARY-ONLY MODE ---
        if SUMMARY_ONLY:
            print("📝 SUMMARY_ONLY=true — collecting names per weekday and posting to Slack…")
            results = []
            for d in weekdays:
                r = check_date_with_auth(browser, d)
                if "error" in r:
                    results.append((d, r["url"], False, [], None, r["error"]))
                else:
                    results.append((d, r["url"], r["available"], r.get("names", []), r.get("deliveries", []), None))
            browser.close()

            # Build Slack blocks:
            # Top line: single "click here to order" pointing at BASE_URL
            blocks = [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🍱 Lunchdrop (weekdays)* — <{BASE_URL}|click here to order>"
                }
            }]

            for d, url, avail, names, deliveries, err in results:
                dow = d.strftime("%a")           # Tue
                pretty = d.strftime("%b %-d") if os.name != "nt" else d.strftime("%b %d")  # Aug 19
                day_label = f"{dow}, {pretty}"
                if err:
                    line = f"*{day_label}* — ⚠️ error: `{err}`"
                elif avail:
                    if names:
                        line = f"*{day_label}* — " + ", ".join(names)
                    else:
                        line = f"*{day_label}* — *(available, names not detected)*"
                else:
                    line = f"*{day_label}* — _not available_"
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

            notify_slack("Lunchdrop summary (weekdays)", blocks)
            print("📣 Posted summary to Slack. Exiting.")
            return

        # --- NORMAL MODE (diff + alerts) ---
        for d in weekdays:
            r = check_date_with_auth(browser, d)
            url = r["url"]
            if "error" in r:
                print(f"⚠️ {r['error']}")
                errors.append(r["error"])
                continue

            snap_now = {
                "available": r["available"],
                "digest": r["digest"],
                "names": r.get("names", []),
                "deliveries": r.get("deliveries", [])
            }
            prev = load_state(url) or {}
            prev_available = prev.get("available")
            prev_digest = prev.get("digest")
            prev_names = set(prev.get("names", []))
            now_names = set(snap_now.get("names", []))

            save_state(url, snap_now)

            became_available = (prev_available in (None, False)) and snap_now["available"]
            changed = prev_digest and prev_digest != snap_now["digest"]
            # names_added = sorted(now_names - prev_names)  # available if you want to show deltas later

            if became_available or (snap_now["available"] and changed):
                newly_available.append((d, url, snap_now.get("names", [])))
                print(f"🎉 ALERT: {d.isoformat()} — names={', '.join(snap_now.get('names', []))}")

        browser.close()

    # ----- Slack (normal mode) -----
    if newly_available:
        # Put a single "click here to order" at the top, then day lines
        blocks = [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🎉 New future Lunchdrop dates available* — <{BASE_URL}|click here to order>"
            }
        }]
        for d, url, names in newly_available:
            dow = d.strftime("%a")
            pretty = d.strftime("%b %-d") if os.name != "nt" else d.strftime("%b %d")
            day_label = f"{dow}, {pretty}"
            line = f"• *{day_label}* — " + (", ".join(names) if names else "_available_")
            blocks.append({"type":"section","text":{"type":"mrkdwn","text": line}})
        notify_slack("New future Lunchdrop dates available", blocks)
        print(f"📣 Notified Slack: {len(newly_available)} date(s)")
    else:
        if SEND_HEARTBEAT:
            blocks = [
                {"type":"section","text":{"type":"mrkdwn","text":"*✅ Lunchdrop monitor ran — no new future menus to report.*"}},
                {"type":"context","elements":[{"type":"mrkdwn","text":f"Window: {weekdays[0]} → {weekdays[-1]} (weekdays only)"}]}
            ]
            notify_slack("Lunchdrop monitor heartbeat — no new menus", blocks)
            print("📣 Sent heartbeat to Slack.")
        else:
            print("ℹ️ No new menus and heartbeat disabled — nothing sent to Slack.")

    for e in errors:
        print(f"[warn] {e}")

if __name__ == "__main__":
    main()
