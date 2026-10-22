#!/usr/bin/env python3
"""Grab a CourtReserve court the instant booking opens.

Usage:
  python book.py              # wait for open_time, then book
  python book.py --now        # skip the wait (good for testing on an already-open day)
  python book.py --date 2026-10-29 --now --dry-run
  python book.py --explore    # log in and open Playwright Inspector to find selectors
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
SHOTS_DIR = HERE / "screenshots"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["account"]["password"] = cfg["account"].get("password") or os.environ.get("CR_PASSWORD", "")
    if not cfg["account"]["password"]:
        sys.exit("No password: set it in config.toml or export CR_PASSWORD.")
    return cfg


def sleep_until(target, label):
    while True:
        remaining = (target - datetime.now(target.tzinfo)).total_seconds()
        if remaining <= 0:
            return
        if remaining > 30:
            log(f"Waiting {remaining:.0f}s until {label} ({target:%a %b %d %H:%M:%S})")
        time.sleep(min(remaining, 30) if remaining > 1 else max(remaining, 0.005))


def login(page, cfg):
    page.goto(cfg["club"]["login_url"], wait_until="domcontentloaded")
    pw_field = page.locator("input[type='password']")
    try:
        pw_field.first.wait_for(timeout=5000)
    except PWTimeout:
        log("No login form -- already signed in (saved session).")
        return
    email_field = page.locator(
        "input[type='email'], input[name*='mail' i], input[name*='user' i], input[id*='mail' i]"
    ).first
    email_field.fill(cfg["account"]["email"])
    pw_field.first.fill(cfg["account"]["password"])
    page.locator("button[type='submit'], input[type='submit'], button:has-text('Log In'), "
                 "button:has-text('Sign In')").first.click()
    page.wait_for_load_state("networkidle")
    log("Logged in.")


def go_to_date(page, cfg, target):
    """Click the scheduler's next-day arrow until the header shows the target date."""
    sel = cfg["selectors"]
    want = target.strftime(sel["date_format"])
    header = page.locator(sel["current_date"]).first
    for _ in range(31):
        try:
            if want.lower() in header.inner_text(timeout=2000).lower():
                return True
        except PWTimeout:
            pass
        page.locator(sel["next_day"]).first.click()
        page.wait_for_timeout(150)
    log(f"Couldn't find date header '{want}'. Check [selectors] current_date/date_format.")
    return False


def find_slot(page, cfg):
    """Return a locator for the best open 'Reserve' button, or None.

    Matches a Reserve button whose own attributes or surrounding row/cell text mention
    the wanted start time (and court, if configured). Tries preferences in order.
    """
    btn_text = cfg["selectors"]["reserve_button_text"]
    courts = cfg["booking"].get("courts") or [""]
    for t in cfg["booking"]["times"]:
        for court in courts:
            idx = page.evaluate(
                """([btnText, time, court]) => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const btns = [...document.querySelectorAll('button, a')]
                        .filter(b => norm(b.innerText) === norm(btnText) && b.offsetParent !== null);
                    for (let i = 0; i < btns.length; i++) {
                        const b = btns[i];
                        const attrs = norm([...b.attributes].map(a => a.value).join(' '));
                        const ctx = norm((b.closest('tr, td, [role=row], [class*=slot]') || b.parentElement).innerText);
                        const hay = attrs + ' ' + ctx;
                        if (hay.includes(norm(time)) && (!court || hay.includes(norm(court)))) {
                            b.setAttribute('data-crbot', 'pick');
                            return i;
                        }
                    }
                    return -1;
                }""",
                [btn_text, t, court],
            )
            if idx >= 0:
                log(f"Found open slot: {t}{' / ' + court if court else ''}")
                return page.locator("[data-crbot='pick']").first
    return None


def fill_modal(page, cfg, dry_run):
    b = cfg["booking"]
    for label, value in (("Reservation Type", b.get("reservation_type")), ("Duration", b.get("duration"))):
        if not value:
            continue
        try:
            page.get_by_label(label, exact=False).select_option(label=value, timeout=1500)
        except Exception:
            # CourtReserve often uses Kendo dropdowns instead of <select>.
            try:
                page.locator(f"text={label}").locator("xpath=following::span[contains(@class,'k-dropdown') or contains(@class,'k-picker')][1]").click(timeout=1500)
                page.locator(f"li:has-text('{value}')").first.click(timeout=1500)
            except Exception:
                log(f"Couldn't set {label} = {value}; leaving default.")

    if dry_run or not b.get("auto_confirm"):
        log("Pop-up is open. Finish it yourself (dry-run or auto_confirm=false).")
        return None

    for txt in cfg["selectors"]["confirm_button_texts"]:
        btn = page.locator(f".modal button:has-text('{txt}'), [role=dialog] button:has-text('{txt}'), "
                           f".k-window button:has-text('{txt}')")
        if btn.count():
            btn.first.click()
            log(f"Clicked '{txt}'.")
            break
    else:
        log("No confirm button found -- click it yourself!")
        return None

    for _ in range(40):
        body = page.locator("body").inner_text().lower()
        if any(s in body for s in cfg["selectors"]["success_texts"]):
            return True
        page.wait_for_timeout(250)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--date", help="Court date YYYY-MM-DD (default: today + days_ahead)")
    ap.add_argument("--now", action="store_true", help="Don't wait for open_time")
    ap.add_argument("--dry-run", action="store_true", help="Open the booking pop-up but never submit")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--explore", action="store_true", help="Log in, then pause in Playwright Inspector")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["club"].get("timezone", "America/New_York"))
    t = cfg["timing"]

    today = datetime.now(tz).date()
    target_date = date.fromisoformat(args.date) if args.date else today + timedelta(days=cfg["booking"]["days_ahead"])
    open_at = datetime.combine(today, datetime.strptime(cfg["booking"]["open_time"], "%H:%M:%S").time(), tz)
    if args.now or args.explore:
        open_at = datetime.now(tz)
    log(f"Target court date: {target_date:%A %Y-%m-%d}; booking opens {open_at:%H:%M:%S}")
    if target_date.weekday() != 2:
        log("Heads up: target date isn't a Wednesday.")

    sleep_until(open_at - timedelta(seconds=t["prewarm_seconds"]), "pre-warm")

    SHOTS_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=args.headless, viewport={"width": 1400, "height": 1000}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        login(page, cfg)
        page.goto(cfg["club"]["booking_url"], wait_until="networkidle")

        if args.explore:
            log("Inspector open. Use 'Pick locator' to grab selectors, then close the browser.")
            page.pause()
            return

        go_to_date(page, cfg, target_date)
        sleep_until(open_at - timedelta(milliseconds=t["start_early_ms"]), "polling start")

        deadline = time.monotonic() + t["give_up_after_seconds"]
        attempt = 0
        slot = None
        while time.monotonic() < deadline:
            attempt += 1
            slot = find_slot(page, cfg)
            if slot:
                break
            page.wait_for_timeout(t["refresh_interval_ms"])
            # Re-render the day: reload, then step back to the target date.
            page.reload(wait_until="domcontentloaded")
            go_to_date(page, cfg, target_date)
            if attempt % 20 == 0:
                log(f"Still no matching slot after {attempt} checks...")

        if not slot:
            page.screenshot(path=str(SHOTS_DIR / "no-slot.png"), full_page=True)
            log("Gave up -- no matching slot. Screenshot saved to screenshots/no-slot.png")
            if not args.headless:
                input("Browser left open. Press Enter to close...")
            return

        slot.click()
        log("Clicked Reserve.")
        page.wait_for_timeout(300)
        result = fill_modal(page, cfg, args.dry_run)
        page.screenshot(path=str(SHOTS_DIR / f"result-{datetime.now():%Y%m%d-%H%M%S}.png"), full_page=True)
        if result:
            log("Booked! Check your email / My Reservations to confirm.")
        elif result is False:
            log("Submitted but didn't see a success message -- check the screenshot / site.")
        if not args.headless:
            input("Press Enter to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
