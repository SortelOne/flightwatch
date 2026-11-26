# Flightwatch

Watches Google Flights for cheap round trips from your home airports to a list of
destinations, and sends a Discord message when a fare drops under your price.

- Searches every matching departure date in your travel window, e.g. every Thursday-to-Tuesday trip from March to June
- Searches all your home airports in one query, e.g. both ORD and MDW if you live in Chicago (set `origins` in the config)
- Respects your limits: travelers, cabin, max stops, and shortest/longest layover
- Keeps price history in SQLite, so alerts can show the lowest price seen before
- Doesn't repeat itself: re-alerts only if the price drops further, or after a few days
- No API keys and no AI model. It uses [fli](https://github.com/punitarani/fli), which reads Google Flights directly

```
✈️ ORD/MDW → LIS for $1,280
Dates    Thu Mar 4 → Tue Mar 9, 2027 (5 nights)
Price    $1,280 round trip for 2 (about $640 each)
         Your target: $1,400 · Lowest seen before: $1,310
Outbound ORD Thu Mar 4 7:05 AM → LIS Fri Mar 5 3:00 AM
         DL 1234, TP 208 · 1 stop · 13h 55m
         Layover in JFK: 1h 40m
Return   ...
```

## Install on a Proxmox LXC

Use a Debian 12 or Ubuntu 24.04 container (Python 3.11+). 512 MB RAM and 1 core
are plenty. Inside the container, as root:

```bash
apt-get update && apt-get install -y git
git clone <this repo> /root/flightwatch    # or copy the folder over
cd /root/flightwatch
./deploy/install.sh
```

Then:

```bash
nano /etc/flightwatch/config.toml        # your dates and destinations
nano /etc/flightwatch/flightwatch.env    # paste your Discord webhook URL
flightwatch check-config                 # shows what will be searched
flightwatch test-discord                 # you should see a message in Discord
flightwatch scan --dry-run --only CUN    # one real search, logged, nothing sent
systemctl start flightwatch.timer        # from now on it scans every 6 hours
```

The test search counts as a scan, so the first scheduled one comes 6 hours after it.

### Setting up the Discord messages

Flightwatch posts through a Discord *webhook*: a private URL that lets it post to
one channel. It's free, and you don't need a bot or a Discord developer account.

1. In Discord, pick (or create) the channel where alerts should go, e.g. `#flight-deals`.
   You need the Manage Webhooks permission, which you have on your own server.
2. Channel settings (the gear next to the channel name) → **Integrations** →
   **Webhooks** → **New Webhook**. Name it "Flightwatch" if you like.
3. Click **Copy Webhook URL**.
4. On the LXC, put it in `/etc/flightwatch/flightwatch.env`:
   ```
   FLIGHTWATCH_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
   ```
5. Run `flightwatch test-discord`. A "Flightwatch is connected" message should appear.

Treat the webhook URL like a password: anyone who has it can post in that channel.
If it leaks, delete the webhook in Discord and make a new one.

## Day to day

| What | Command |
|---|---|
| Add or remove a destination | edit `[destinations]` in `/etc/flightwatch/config.toml`; the next scan picks it up |
| See prices so far, and when the next scan is due | `flightwatch status` |
| Change how often it scans | set `scan_every_hours` in `[scanner]` in the config |
| Scan right now, even if one ran recently | `flightwatch scan --now` |
| Watch the logs | `journalctl -u flightwatch -f` |
| Update Flightwatch | `git pull && ./deploy/install.sh` (keeps your config) |

### How the schedule works

The timer runs `flightwatch scan` every 10 minutes, but Flightwatch only scans if
the last scan *started* at least `scan_every_hours` ago (6 by default). Most of
those checks just exit at once. The start time is saved in the database before the
first request to Google, so a crash, a reboot, a restart or a re-install can never
cause an extra scan. That includes scans you run by hand: `--dry-run` and `--only`
tests count too, and only `--now` skips the wait.

## The config

```toml
origins = ["ORD", "MDW"]           # your home airport(s), searched together

[trip]
adults = 2
max_stops = 1
min_layover_minutes = 90           # no layover shorter than 1h 30m
max_layover_minutes = 360          # or longer than 6 hours
earliest_departure = "2027-03-01"
latest_return      = "2027-06-30"  # be home by this date
depart_weekdays = ["thu"]          # leave on a Thursday...
nights = [5]                       # ...for 5 nights, so home on Tuesday

[destinations]
LIS = 1400        # round-trip total for both travelers
CUN = 700
NRT = { max_price = 1800, depart_weekdays = ["fri"], nights = [9] }   # override anything per destination

[scanner]
scan_every_hours = 6   # at most one scan per 6 hours
paused = false         # true = stop scanning for now
```

For any day of the week, set `depart_weekdays = []` and list the trip lengths you'd
take, e.g. `nights = [5, 6, 7]`. Then run `flightwatch check-config`: it spells out
in plain English what will be searched, so you can catch mistakes.

The prices are what Google Flights shows for the whole party. The first time you
get an alert, open its link and check the total matches what you expect.

## How many searches is that?

Each departure date × trip length is one Google page load. `check-config` prints
the total. Thursday-to-Tuesday trips from March to June are 17 dates, so 3
destinations come to about 69 page loads per scan. "Any day, 5–7 nights" over the
same months would be about 1,000. Flightwatch spaces them out (2 per second by default, with pauses
between destinations). If Google starts blocking you, shrink the date window, use
fewer trip lengths, or raise `scan_every_hours`.

## If searches start failing

fli is unofficial, so a change on Google's side can break it. You'll get one
Discord warning a day while every search is failing. To fix it, update fli:

```bash
/opt/flightwatch/venv/bin/pip install -U flights
flightwatch scan --dry-run --only CUN --now
```

If that fixes it, bump the `flights==` pin in `pyproject.toml` to the version that
worked (`/opt/flightwatch/venv/bin/pip show flights`). Otherwise the next
`./deploy/install.sh` puts the old, broken version back.

If the service won't start in the LXC (`status=226/NAMESPACE` in the logs),
turn on Options → Features → Nesting for the container in Proxmox, or delete the
`Protect*`/`Private*` lines in the service file.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
