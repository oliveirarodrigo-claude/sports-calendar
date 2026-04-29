# Sports Calendar

Automatically syncs sports events into Apple Calendar ("Sports on TV") and serves a local dashboard at `http://localhost:8765`.

**Supported sports:**
- ⚽️ Your soccer team (Brasileirão, Libertadores, Copa do Brasil) — via ESPN + CBF PDF
- 🎾 An ATP tennis player — via ESPN Core API
- 🏆 FIFA World Cup 2026 — via ESPN (activates Jun 24)
- 🎮 An esports team (CS2) — via Liquipedia

---

## Requirements

- macOS (uses EventKit, LaunchAgents — Mac only)
- Python 3.9+
- Xcode Command Line Tools (`xcode-select --install`)
- Python packages: `playwright`, `pypdf`

```bash
pip3 install playwright pypdf
python3 -m playwright install chromium
```

---

## Setup

### 1. Clone
```bash
git clone https://github.com/YOUR_USERNAME/sports-calendar.git
cd sports-calendar
```

### 2. Configure your teams

Edit `cruzeiro_calendar.py` and update these constants at the top:

```python
CRUZEIRO_ESPN_ID = "2022"       # Your soccer team's ESPN ID
FONSECA_ESPN_ID  = "11745"      # Your tennis player's ESPN ID
FURIA_LP_PAGE    = "FURIA"      # Your CS2 team's Liquipedia page name
```

**Finding ESPN IDs:**
Go to `https://site.api.espn.com/apis/site/v2/sports/soccer/bra.1/teams` and search for your team. The `id` field is what you need. For tennis, search `https://sports.core.api.espn.com/v2/sports/tennis/leagues/atp/athletes?limit=100&search=YOUR_PLAYER`.

Also update `cruzeiro_dashboard.py` with your team's ESPN ID:
```python
CRUZEIRO_ESPN_ID = "2022"
```

### 3. Create the Apple Calendar

Open Calendar.app and create a calendar named **"Sports on TV"** (must be in iCloud).

### 4. Compile the Swift binary

```bash
cd "~/Library/Application Support/CruzeiroCalendar"
# First, copy the Swift file there:
mkdir -p ~/Library/Application\ Support/CruzeiroCalendar
cp update_calendar.swift ~/Library/Application\ Support/CruzeiroCalendar/
cd ~/Library/Application\ Support/CruzeiroCalendar
swiftc update_calendar.swift -o update_calendar -framework EventKit
```

### 5. Install LaunchAgents

Copy the three plist files to `~/Library/LaunchAgents/` and load them:

```bash
cp LaunchAgents/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.cruzeiro.calendar.fast.plist
launchctl load ~/Library/LaunchAgents/com.cruzeiro.calendar.soccer.plist
launchctl load ~/Library/LaunchAgents/com.cruzeiro.calendar.worldcup.plist
```

### 6. Set up the dashboard server

```bash
mkdir -p ~/Library/Application\ Support/CruzeiroCalendar
cp serve_ics.py ~/Library/Application\ Support/CruzeiroCalendar/serve_ics.py
```

Edit `~/Library/Application Support/CruzeiroCalendar/serve_ics.py` and update `BASE_DIR` to point to your cloned repo directory.

Then load the server LaunchAgent:
```bash
cp LaunchAgents/com.cruzeiro.server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.cruzeiro.server.plist
```

### 7. First run

```bash
python3 cruzeiro_calendar.py
```

This seeds the calendar and all dashboards. Open `http://localhost:8765` in your browser and bookmark it.

---

## How it works

| Job | Schedule | What it does |
|---|---|---|
| `--fast` | Every 5 min | Tennis + CS2 — updates every 5 min if game today/tomorrow, otherwise once/day |
| `--soccer` | Sundays 09:00 | Cruzeiro fixtures only (CBF PDF + ESPN) |
| `--worldcup` | Hourly | World Cup bracket (active from Jun 24 2026) |
| Dashboard server | Always on | Serves `localhost:8765` |

### Deletion rule
Only **future** events are ever deleted and re-written. Once a game is played it stays in your calendar permanently.

### Change tracking
Every time an event changes (reschedule, TBD → confirmed time, new entry) it's logged to `calendar_changes.json` and visible at `localhost:8765/calendar_changelog.html`.

---

## Dashboard pages

| URL | Content |
|---|---|
| `localhost:8765` | Hub — links to all pages |
| `localhost:8765/cruzeiro_results.html` | Soccer results & schedule |
| `localhost:8765/fonseca.html` | Tennis season results |
| `localhost:8765/furia_results.html` | CS2 results & schedule |
| `localhost:8765/calendar_changelog.html` | Calendar change log |

---

## Data sources

| Sport | Source |
|---|---|
| Soccer | ESPN public API + CBF PDF (Brasileirão official schedule) |
| Tennis | ESPN Core ATP API (athlete eventlog) |
| World Cup | ESPN public API |
| CS2 | Liquipedia MediaWiki API |

---

## File structure

```
cruzeiro_calendar.py      # Main sync script (all sports, all modes)
cruzeiro_dashboard.py     # Generates cruzeiro_results.html
fonseca_dashboard.py      # Generates fonseca.html
furia_dashboard.py        # Generates furia_results.html
update_calendar.swift     # EventKit binary (compile once)
serve_ics.py              # Local HTTP server (copy to ~/Library/Application Support/CruzeiroCalendar/)
index.html                # Dashboard hub
changelog.html            # Build changelog
LaunchAgents/             # macOS LaunchAgent plists
```
