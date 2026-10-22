#!/usr/bin/env python3
"""Grab CourtReserve courts the instant booking opens.

Each [[booking.slots]] entry in config.toml is booked by its own browser window, all
running at the same time, so slot 2 isn't waiting on slot 1.

Usage:
  python book.py              # wait for open_time, then book every slot
  python book.py --now        # skip the wait (test on a day that's already open)
  python book.py --date 2026-10-29 --now --dry-run
  python book.py --only 1     # run just the first slot (1-based)
  python book.py --explore    # log in and open Playwright Inspector to find selectors
"""
import argparse
import os
import subprocess
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
SHOTS_DIR = HERE / "screenshots"
TAG = ""


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}]{TAG} {msg}", flush=True)


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["account"]["password"] = cfg["account"].get("password") or os.environ.get("CR_PASSWORD", "")
    if not cfg["account"]["password"]:
        sys.exit("No password: set it in config.toml or export CR_PASSWORD.")
    if not cfg["booking"].get("slots"):
        sys.exit("No [[booking.slots]] in config.toml.")
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
    page.locator(
        "input[type='email'], input[name*='mail' i], input[name*='user' i], input[id*='mail' i]"
    ).first.fill(cfg["account"]["email"])
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


def find_slot(page, cfg, slot):
    """Return a locator for the best open 'Reserve' button for this slot, or None.

    A button matches a time/court if its own attributes or surrounding row/cell text
    mention it, or if it lines up on screen with a label showing exactly that text
    (court names are usually column headers, times are row labels down the side).
    Tries preferences in order.
    """
    btn_text = cfg["selectors"]["reserve_button_text"]
    courts = slot.get("courts") or cfg["booking"].get("courts") or [""]
    for t in slot["times"]:
        for court in courts:
            idx = page.evaluate(
                """([btnText, time, court]) => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const visible = el => el.offsetParent !== null;
                    // Leaf-ish elements whose whole text is exactly `text`.
                    const labels = text => [...document.querySelectorAll('th, td, div, span, label')]
                        .filter(el => visible(el) && norm(el.innerText) === norm(text))
                        .map(el => (el.closest('th, td') || el).getBoundingClientRect());
                    const timeLabels = labels(time);
                    const courtLabels = court ? labels(court) : [];
                    const btns = [...document.querySelectorAll('button, a')]
                        .filter(b => norm(b.innerText) === norm(btnText) && visible(b));
                    for (let i = 0; i < btns.length; i++) {
                        const b = btns[i];
                        const r = b.getBoundingClientRect();
                        const cx = (r.left + r.right) / 2;
                        const attrs = norm([...b.attributes].map(a => a.value).join(' '));
                        const ctx = norm((b.closest('tr, td, [role=row], [class*=slot]') || b.parentElement).innerText);
                        const hay = attrs + ' ' + ctx;
                        const timeOk = hay.includes(norm(time)) ||
                            timeLabels.some(l => l.top < r.bottom && l.bottom > r.top);
                        const courtOk = !court || new RegExp('(^|\\\\W)' + norm(court) + '($|\\\\W)').test(hay) ||
                            courtLabels.some(l => l.left <= cx && l.right >= cx);
                        if (timeOk && courtOk) {
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


def set_dropdown(page, label, value):
    """Pick `value` in the field labelled `label` (native <select> or Kendo dropdown)."""
    try:
        page.get_by_label(label, exact=False).select_option(label=value, timeout=1500)
        return True
    except Exception:
        pass
    try:
        page.locator(f"text={label}").first.locator(
            "xpath=following::*[contains(@class,'k-dropdown') or contains(@class,'k-picker')"
            " or contains(@class,'k-combobox')][1]"
        ).click(timeout=1500)
        page.locator(f"li:visible:has-text('{value}')").first.click(timeout=1500)
        return True
    except Exception:
        log(f"Couldn't set {label} = {value}; leaving default.")
        return False


def fill_modal(page, cfg, slot, dry_run):
    b, sel = cfg["booking"], cfg["selectors"]
    fields = (
        (sel["member_field_label"], slot.get("member")),
        (sel["reservation_type_label"], b.get("reservation_type")),
        (sel["duration_label"], slot.get("duration") or b.get("duration")),
    )
    for label, value in fields:
        if value:
            set_dropdown(page, label, value)

    if dry_run or not b.get("auto_confirm"):
        log("Pop-up is open. Finish it yourself (dry-run or auto_confirm=false).")
        return None

    for txt in sel["confirm_button_texts"]:
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
        if any(s in body for s in sel["success_texts"]):
            return True
        page.wait_for_timeout(250)
    return False


def keep_open(page, headless):
    """Leave the window up for a human until they close it."""
    if headless:
        return
    log("Leaving the window open -- close it when you're done.")
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:
        pass


def run_slot(cfg, slot_no, args):
    global TAG
    slot = cfg["booking"]["slots"][slot_no - 1]
    TAG = f" [slot {slot_no}: {slot['times'][0]}{' / ' + slot['member'] if slot.get('member') else ''}]"
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
        # Each slot needs its own profile dir: Chrome won't share one between windows.
        ctx = p.chromium.launch_persistent_context(
            str(HERE / f"browser-profile-{slot_no}"), headless=args.headless,
            viewport={"width": 1400, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        login(page, cfg)
        page.goto(cfg["club"]["booking_url"], wait_until="networkidle")

        if args.explore:
            log("Inspector open. Use 'Pick locator' to grab selectors, then close the browser.")
            page.pause()
            return 0

        go_to_date(page, cfg, target_date)
        sleep_until(open_at - timedelta(milliseconds=t["start_early_ms"]), "polling start")
        started = time.monotonic()

        deadline = started + t["give_up_after_seconds"]
        attempt = 0
        found = None
        while time.monotonic() < deadline:
            attempt += 1
            found = find_slot(page, cfg, slot)
            if found:
                break
            page.wait_for_timeout(t["refresh_interval_ms"])
            # Re-render the day: reload, then step back to the target date.
            page.reload(wait_until="domcontentloaded")
            go_to_date(page, cfg, target_date)
            if attempt % 20 == 0:
                log(f"Still no matching slot after {attempt} checks...")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if not found:
            page.screenshot(path=str(SHOTS_DIR / f"no-slot-{slot_no}-{stamp}.png"), full_page=True)
            log("Gave up -- no matching slot. Screenshot saved in screenshots/.")
            keep_open(page, args.headless)
            return 1

        found.click()
        log(f"Clicked Reserve ({time.monotonic() - started:.2f}s after polling started).")
        page.wait_for_timeout(300)
        result = fill_modal(page, cfg, slot, args.dry_run)
        page.screenshot(path=str(SHOTS_DIR / f"result-{slot_no}-{stamp}.png"), full_page=True)
        log(f"Done in {time.monotonic() - started:.2f}s after polling started.")
        if result:
            log("BOOKED! Check your email / My Reservations to confirm.")
            ctx.close()
            return 0
        if result is False:
            log("Submitted but didn't see a success message -- check the screenshot / site.")
        keep_open(page, args.headless)
        ctx.close()
        return 0 if result is None else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--date", help="Court date YYYY-MM-DD (default: today + days_ahead)")
    ap.add_argument("--now", action="store_true", help="Don't wait for open_time")
    ap.add_argument("--dry-run", action="store_true", help="Open the booking pop-up but never submit")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--explore", action="store_true", help="Log in, then pause in Playwright Inspector")
    ap.add_argument("--only", type=int, help="Run just this slot number (1-based)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    n = len(cfg["booking"]["slots"])

    if args.only or args.explore or n == 1:
        sys.exit(run_slot(cfg, args.only or 1, args))

    # One process per slot so they all hit the site at the same instant.
    child_args = [a for a in sys.argv[1:]]
    procs = [subprocess.Popen([sys.executable, __file__, *child_args, "--only", str(i)])
             for i in range(1, n + 1)]
    codes = [proc.wait() for proc in procs]
    print("\nSummary:", ", ".join(f"slot {i}: {'ok' if c == 0 else 'FAILED'}"
                                 for i, c in enumerate(codes, 1)))
    sys.exit(max(codes))


if __name__ == "__main__":
    main()
