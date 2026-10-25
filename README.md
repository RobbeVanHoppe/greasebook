# GreaseBook

A self-hosted garage ledger for people who maintain their own cars and motorcycles.
Flask + SQLite, no build step, works on desktop and phone.

Track what's due, what you fitted, which part number to reorder, and how deep into
the money pit you are.

## Features

- **Multiple vehicles**, cars and motorcycles, each with its own odometer
- **Service schedule with life bars** - km and/or month intervals per item; the bar
  shows whichever clock runs out first. Green, orange from 75%, hazard-striped red when overdue
- **Maintenance sessions (work orders)** - parts used, labour cost, notes, photo/invoice attachments
- **Life bars derive from your sessions** - tick which schedule items a session covered and the
  bars recalculate from the newest covering session. Edit or delete a session and they roll back
  automatically; nothing is overwritten destructively
- **Parts catalogue and suppliers** with part numbers and prices for reordering; create a part
  or supplier inline while logging a session
- **Fuel log** with l/100 km calculated between full tanks
- **Search and filters** - sessions by title, notes, part, part number, supplier and date range
- **Exports** - PDF service history with a full cost summary (parts, labour, fuel, grand total,
  cost per km and per year, biggest spends), plus CSV for sessions, fuel and the parts catalogue
- **Mobile-first layout** - tables reflow into cards, drawer navigation, native date pickers
- Optional single password, journald-friendly logging, hardened systemd unit

## Quick start

### Bare metal / VM / LXC (recommended)

```bash
sudo bash install.sh
```

Creates a virtualenv in `/opt/greasebook`, a dedicated service user, and a systemd unit that
starts at boot. Data lives in `/var/lib/greasebook`. The script prints the URL when it's done.

```bash
systemctl status greasebook
journalctl -u greasebook -f
```

### Docker / Podman

```bash
docker compose up --build -d
# or, plain podman:
podman build -t greasebook .
podman run -d --name greasebook -p 8000:8000 -v ./greasebook-data:/data:Z greasebook
```

> Running **inside an LXC container**? Skip nested containers and use `install.sh`. Rootless
> Podman inside LXC runs into nested user-namespace and AppArmor restrictions.

### Try it out by hand

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 app.py            # http://localhost:8000 (debug mode)
```

For anything beyond a quick look, use `waitress-serve --host=0.0.0.0 --port=8000 app:app`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GREASEBOOK_DATA` | `~/.greasebook` (`/var/lib/greasebook` via `install.sh`, `/data` in the image) | Database and attachments folder |
| `GREASEBOOK_PASSWORD` | unset | If set, the whole app requires this password |

There is **no built-in HTTPS and no multi-user support**. If you expose it beyond your LAN,
put it behind a reverse proxy with TLS (Caddy needs about three lines) and set a password.

## Data and backup

Everything is in the data folder: `garage.db` (SQLite) plus `attachments/`. Copy that folder and
you have a complete backup.

```bash
sudo tar czf greasebook-backup-$(date +%F).tar.gz -C /var/lib greasebook
```

Pages embed database IDs in their dropdowns, so HTML responses are sent with `no-store`.
After swapping the database underneath a running instance, hard-reload your browser.

## Updating

```bash
sudo cp -r app.py templates static /opt/greasebook/
sudo systemctl restart greasebook
```

The schema is created with `CREATE TABLE IF NOT EXISTS`, and your data is never in the app folder.

## Project layout

```
app.py              Flask app, SQLite access, PDF writer, all routes
templates/          Jinja templates (dashboard, sessions, fuel, catalog, export)
static/style.css    Instrument-panel theme, mobile-first
static/app.js       Dialogs, drawer, parts-line editor, inline part/supplier creation
install.sh          Bare-metal installer (venv + systemd)
Dockerfile, docker-compose.yml
tests/smoke_test.py End-to-end smoke test against a throwaway database
```

## Tests

```bash
python3 tests/smoke_test.py
```

Runs against a temporary data folder and never touches your real ledger.

## Notes

- The PDF is produced by a small built-in writer (no dependencies), using WinAnsi encoding
  so the euro sign renders correctly.
- Fonts (Chakra Petch, JetBrains Mono) load from Google Fonts; the app falls back to system fonts offline.

## License

MIT - see [LICENSE](LICENSE).
