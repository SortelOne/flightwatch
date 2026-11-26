# Flightwatch

Watches Google Flights for cheap round trips from your home airports to a list of
destinations, and sends a Discord message when a fare drops under your price.

- Searches every departure date in your travel window, for each trip length you list
- Searches both home airports in one query (ORD and MDW by default)
- Respects your limits: travelers, cabin, max stops, and max layover length
- Keeps price history in SQLite, so alerts can show the lowest price seen before
- Doesn't repeat itself: re-alerts only if the price drops further, or after a few days
- No API keys and no AI model. It uses [fli](https://github.com/punitarani/fli), which reads Google Flights directly

```
✈️ ORD/MDW → LIS for $1,280
Dates    Wed Mar 3 → Wed Mar 10, 2027 (7 nights)
Price    $1,280 round trip for 2 (about $640 each)
         Your target: $1,400 · Lowest seen before: $1,310
Outbound ORD Wed Mar 3 7:05 AM → LIS Thu Mar 4 3:00 AM
         DL 1234, TP 208 · 1 stop · 15h 00m
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

To make a Discord webhook: Server Settings → Integrations → Webhooks → New
Webhook → pick a channel → Copy Webhook URL.

## Day to day

| What | Command |
|---|---|
| Add or remove a destination | edit `[destinations]` in `/etc/flightwatch/config.toml`; the next scan picks it up |
| See prices so far | `flightwatch status` |
| Run a scan right now | `systemctl start flightwatch.service` |
| Watch the logs | `journalctl -u flightwatch -f` |
| When's the next scan | `systemctl list-timers flightwatch.timer` |
| Change how often it runs | edit `OnUnitActiveSec=` in `/etc/systemd/system/flightwatch.timer`, then `systemctl daemon-reload` |
| Update Flightwatch | `git pull && ./deploy/install.sh` (keeps your config) |

## The config

```toml
origins = ["ORD", "MDW"]

[trip]
adults = 2
max_stops = 1
max_layover_minutes = 180
earliest_departure = "2027-03-01"
latest_departure   = "2027-04-30"
nights = [6, 7, 8]

[destinations]
LIS = 1400        # round-trip total for both travelers
CUN = 700
NRT = { max_price = 1800, nights = [10, 14] }   # override anything per destination
```

The prices are what Google Flights shows for the whole party. The first time you
get an alert, open its link and check the total matches what you expect.

## How many searches is that?

Each departure date × trip length is one Google page load. `check-config` prints
the total: for example, 3 destinations × 61 dates × 3 trip lengths ≈ 550 page
loads per scan. Flightwatch spaces them out (2 per second by default, with pauses
between destinations). If Google starts blocking you, shrink the date window, use
fewer trip lengths, or scan less often.

## If searches start failing

fli is unofficial, so a change on Google's side can break it. You'll get one
Discord warning a day while every search is failing. To fix it, update fli:

```bash
/opt/flightwatch/venv/bin/pip install -U flights
flightwatch scan --dry-run --only CUN
```

If the service won't start in the LXC (`status=226/NAMESPACE` in the logs),
turn on Options → Features → Nesting for the container in Proxmox, or delete the
`Protect*`/`Private*` lines in the service file.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
