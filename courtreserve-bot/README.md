# CourtReserve auto-booker

Logs into CourtReserve a minute or two before courts release, sits on the right day,
and clicks **Reserve** on your preferred time the instant it appears. It runs a real
Chrome window on your Mac, so you can watch it and take over if anything goes wrong.

> Check your club's rules first. Some clubs prohibit automated booking and can
> suspend accounts. This is built to book **one** court for you, not to hoard slots.

## One-time setup (Mac)

```bash
cd ~/courtreserve-bot            # wherever you put this folder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.toml config.toml   # then edit it
export CR_PASSWORD='your-password'   # or put it in config.toml
```

In `config.toml` set:
- `login_url` / `booking_url`: copy them from your browser's address bar while logged in.
- `days_ahead` + `open_time`: when your club releases Wednesday courts (e.g. 7 days ahead at 8:00 AM).
- `times` / `courts`: what you want, in order of preference, written exactly as the site shows them.

## Tune it once (important)

I couldn't test this against your club's page, so do a practice run on a day that's already open:

```bash
python book.py --date 2026-10-29 --now --dry-run
```

It should log in, go to that date, and open the Reserve pop-up without submitting. If it
gets stuck, run `python book.py --explore`, click **Pick locator** in the Inspector, hover
the element it missed (date arrow, date header, Reserve button), and paste that into
`[selectors]` in `config.toml`.

## Booking day

- **By hand:** run `python book.py` a few minutes before open and leave it alone.
- **Automatically:** edit `com.pickleball.courtreserve.plist` (paths and time), then
  ```bash
  cp com.pickleball.courtreserve.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.pickleball.courtreserve.plist
  ```
  Your Mac has to be awake and logged in. `sudo pmset repeat wakeorpoweron W 07:50:00` wakes it.

Leave `auto_confirm = false` at first: the bot gets you to the pop-up in a fraction of a
second and you click Save. Switch it to `true` once you trust it.
