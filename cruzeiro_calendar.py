#!/usr/bin/env python3
"""
Cruzeiro Calendar Generator
- Brasileirão times: sourced from the official CBF PDF (fetched dynamically)
- Libertadores / Copa do Brasil times: sourced from ESPN public API
- TBD games (no confirmed time): stored as all-day events
- All times converted from UTC/BRT → stored as UTC in ICS
"""

import json
import re
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Config ────────────────────────────────────────────────────────────────────

CRUZEIRO_ESPN_ID = "2022"
FONSECA_ESPN_ID  = "11745"
ATP_CORE_BASE    = "http://sports.core.api.espn.com/v2/sports/tennis/leagues/atp"

COMPETITIONS = [
    {"league": "bra.1",                 "name": "Brasileirão Série A", "emoji": "🇧🇷"},
    {"league": "conmebol.libertadores",  "name": "Copa Libertadores",   "emoji": "🌎"},
    {"league": "bra.copa_do_brazil",     "name": "Copa do Brasil",      "emoji": "🏆"},
]

OUTPUT_FILE   = Path(__file__).parent / "cruzeiro.ics"
# Mirror copy served by the local HTTP server (launchd can't access OneDrive)
OUTPUT_MIRROR = Path.home() / "Library/Application Support/CruzeiroCalendar/cruzeiro.ics"
PDF_CACHE     = Path(__file__).parent / "cbf_pdf_url.txt"   # stores last known PDF URL
ESPN_BASE     = "https://site.api.espn.com/apis/site/v2/sports/soccer"
CBF_TABELA    = "https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-a/2026?doc=Tabela%20Detalhada"
DAYS_AHEAD    = 180
BRT           = timezone(timedelta(hours=-3))

# ── CBF PDF fetch & parse ─────────────────────────────────────────────────────

def find_cbf_pdf_url():
    """
    Fetch the current CBF Tabela Detalhada PDF URL using Playwright headless browser.
    Falls back to the cached URL if Playwright fails.
    Automatically saves the latest URL to cbf_pdf_url.txt for future runs.
    """
    print("  Opening CBF page (headless) to find latest PDF link...")
    found_url = None

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(CBF_TABELA, wait_until="networkidle", timeout=20000)
            # Grab all PDF links — pick the one with "Tabela_Detalhada"
            all_pdfs = page.eval_on_selector_all(
                'a[href*=".pdf"]', 'els => els.map(e => e.href)'
            )
            browser.close()

        tabela_pdfs = [u for u in all_pdfs if "Tabela_Detalhada" in u]
        if tabela_pdfs:
            found_url = tabela_pdfs[-1]   # take the last (most recent) if multiple
            cached    = PDF_CACHE.read_text().strip() if PDF_CACHE.exists() else ""
            if found_url != cached:
                print(f"  🆕 New PDF detected: {found_url}")
            else:
                print(f"  ✅ PDF URL confirmed (unchanged): {found_url}")
            PDF_CACHE.write_text(found_url)
        else:
            print("  ⚠️  No Tabela_Detalhada PDF found on CBF page.", file=sys.stderr)

    except Exception as e:
        print(f"  ⚠️  Playwright failed: {e}", file=sys.stderr)

    # Fall back to cache
    if not found_url:
        if PDF_CACHE.exists():
            found_url = PDF_CACHE.read_text().strip()
            print(f"  📋 Using cached PDF URL (age: {cached_pdf_age_days()}d): {found_url}")
        else:
            print("  ❌ No PDF URL available. Brasileirão times will be TBD.", file=sys.stderr)

    return found_url


def parse_cbf_pdf(pdf_url: str) -> dict:
    """
    Download and parse the CBF Tabela Detalhada PDF.
    Returns: {round_num: {"date_brt": "YYYY-MM-DD", "time_brt": "HH:MM"|None, "venue": str}}
    """
    print("  Downloading CBF PDF...")
    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            pdf_bytes = r.read()
    except Exception as e:
        print(f"  ⚠️  Could not download CBF PDF: {e}", file=sys.stderr)
        return {}

    pdf_path = Path("/tmp/cbf_tabela_cruzeiro.pdf")
    pdf_path.write_bytes(pdf_bytes)

    try:
        from pypdf import PdfReader
    except ImportError:
        import subprocess, sys as _sys
        subprocess.run([_sys.executable, "-m", "pip", "install", "pypdf", "-q"], check=True)
        from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)

    # The CBF PDF has one game per line. Lines with Cruzeiro look like:
    #   Confirmed: "130 13ª 25/04 sáb 18:30 Remo PA x Cruzeiro MG Baenão Belém PA"
    #   TBD:       "176 18    Cruzeiro MG x Fluminense RJ"
    #
    # Line pattern breakdown:
    #   \d+           = internal match ID (3 digits)
    #   \d{1,2}[ª]?  = round number
    #   dd/mm         = date (optional — empty means TBD)
    #   \w+           = weekday abbreviation (optional)
    #   HH:MM         = kick-off time in BRT (optional — empty means TBD)
    #   ... Cruzeiro ...

    line_re = re.compile(
        r'^\s*\d+\s+'                              # internal match ID
        r'(\d{1,2})[ªa]?\s+'                       # round number
        r'(?:(\d{2}/\d{2})\s+\w+\s+'              # date dd/mm + weekday  (optional)
        r'(\d{2}:\d{2})\s+)?'                      # time HH:MM            (optional)
        r'(.*Cruzeiro.+)$',                         # rest of line — must contain Cruzeiro
        re.IGNORECASE
    )

    results = {}
    now = datetime.now()

    for raw_line in full_text.splitlines():
        line = raw_line.strip()
        if "Cruzeiro" not in line:
            continue
        m = line_re.match(line)
        if not m:
            continue

        round_num = int(m.group(1))
        date_str  = m.group(2)   # "25/04" or None
        time_str  = m.group(3)   # "18:30" or None (TBD if None)
        teams_raw = m.group(4).strip()

        if date_str:
            day, month = date_str.split("/")
            year = now.year
            if int(month) < now.month - 2:
                year += 1
            date_iso = f"{year}-{int(month):02d}-{int(day):02d}"
        else:
            date_iso = None

        results[round_num] = {
            "date_brt": date_iso,
            "time_brt": time_str,    # None → TBD, CBF left it blank
            "venue":    teams_raw,
        }

    print(f"  ✅ Parsed {len(results)} Cruzeiro fixtures from CBF PDF")
    return results


def cached_pdf_age_days() -> int:
    """Return how many days old the cached PDF URL is, based on the date in its filename."""
    if not PDF_CACHE.exists():
        return 999
    url = PDF_CACHE.read_text().strip()
    # URL contains date like _22_04_ (day_month) or _2026_22_04_
    m = re.search(r'_(\d{2})_(\d{2})_', url)
    if not m:
        return 999
    day, month = int(m.group(1)), int(m.group(2))
    now = datetime.now()
    year = now.year
    try:
        pdf_date = datetime(year, month, day)
        if pdf_date > now:          # date is in future → it's from last year
            pdf_date = datetime(year - 1, month, day)
        return (now - pdf_date).days
    except ValueError:
        return 999


def refresh_pdf_url_via_chrome():
    """
    Use the Chrome MCP (via subprocess calling this script with --get-pdf-url)
    to extract the live PDF download link from the CBF page.
    This is a placeholder — run `python3 cruzeiro_calendar.py --refresh` manually
    when you know CBF has updated the PDF.
    """
    print("  ℹ️  To refresh the PDF URL, run:")
    print(f"     python3 {Path(__file__).name} --refresh")
    return None


def get_cbf_schedule() -> dict:
    """
    Main entry: find PDF URL via Playwright → parse → return schedule dict.
    Returns {} on any failure — caller falls back to ESPN times.
    """
    pdf_url = find_cbf_pdf_url()
    if not pdf_url:
        print("  ⚠️  CBF PDF unavailable — Brasileirão will use ESPN times as fallback.",
              file=sys.stderr)
        return {}
    try:
        return parse_cbf_pdf(pdf_url)
    except Exception as e:
        print(f"  ⚠️  CBF PDF parse error: {e} — falling back to ESPN times.", file=sys.stderr)
        return {}

# ── ESPN fetch (for Libertadores + Copa do Brasil) ────────────────────────────

def fetch_espn_scoreboard(league: str) -> list[dict]:
    now      = datetime.now(timezone.utc)
    end_date = now + timedelta(days=DAYS_AHEAD)
    date_str = f"{now.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}"
    url = f"{ESPN_BASE}/{league}/scoreboard?dates={date_str}&limit=100"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {league} — skipping", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Error fetching {league}: {e}", file=sys.stderr)
        return []

    events   = data.get("events", [])
    now_utc  = datetime.now(timezone.utc)
    games    = []

    for event in events:
        if "Cruzeiro" not in event.get("name", ""):
            continue

        date_raw = event.get("date", "")
        if not date_raw:
            continue
        try:
            kickoff = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff <= now_utc:
            continue

        competitors = event.get("competitions", [{}])[0].get("competitors", [])
        home_team   = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_team   = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_team or not away_team:
            continue

        home_id   = home_team.get("id", "")
        home_name = home_team.get("team", {}).get("displayName", "?")
        away_name = away_team.get("team", {}).get("displayName", "?")
        is_home   = home_id == CRUZEIRO_ESPN_ID
        opponent  = away_name if is_home else home_name

        venue_info = event.get("competitions", [{}])[0].get("venue", {})
        venue_name = venue_info.get("fullName", "")
        venue_city = venue_info.get("address", {}).get("city", "")
        venue_str  = ", ".join(filter(None, [venue_name, venue_city])) or "A definir"

        note_list  = event.get("competitions", [{}])[0].get("notes", [])
        round_name = note_list[0].get("headline", "") if note_list else ""
        event_id   = event.get("id", kickoff.strftime("%Y%m%d%H%M"))

        # Detect ESPN placeholder times: games that have exactly :00 minutes and
        # a suspiciously generic hour (ESPN uses 18:00Z as filler for unscheduled games)
        espn_is_placeholder = (
            kickoff.minute == 0 and kickoff.second == 0
            and kickoff.hour in (18,)   # 18:00Z = 15:00 BRT, not a real CBF kickoff time
        )

        games.append({
            "id":          event_id,
            "kickoff":     kickoff,
            "kickoff_tbd": espn_is_placeholder,
            "home_name":   home_name,
            "away_name":   away_name,
            "is_home":     is_home,
            "opponent":    opponent,
            "venue":       venue_str,
            "round":       round_name,
        })

    return games


def fetch_all_fixtures(cbf_schedule: dict) -> list[tuple[dict, dict]]:
    all_fixtures = []
    now_utc = datetime.now(timezone.utc)

    for comp in COMPETITIONS:
        print(f"\n  Fetching {comp['name']}...")

        if comp["league"] == "bra.1":
            espn_games = fetch_espn_scoreboard(comp["league"])
            added = set()

            if cbf_schedule:
                # ── PRIMARY: CBF PDF is available — use it as source of truth ─
                for round_num, cbf in sorted(cbf_schedule.items()):
                    espn_match = next(
                        (g for g in espn_games
                         if g["id"] not in added
                         and cbf.get("date_brt")
                         and g["kickoff"].astimezone(BRT).strftime("%Y-%m-%d") == cbf["date_brt"]),
                        None
                    )

                    if cbf["date_brt"] is None:
                        # Fully TBD — still include if ESPN knows the opponent
                        espn_match = next(
                            (g for g in espn_games if g["id"] not in added), None
                        )
                        if not espn_match:
                            continue

                    if not espn_match:
                        continue

                    added.add(espn_match["id"])
                    game = dict(espn_match)

                    # Override time with CBF data
                    if cbf["date_brt"] and cbf["time_brt"]:
                        h, mn = map(int, cbf["time_brt"].split(":"))
                        year, month, day = map(int, cbf["date_brt"].split("-"))
                        kickoff_brt = datetime(year, month, day, h, mn, tzinfo=BRT)
                        game["kickoff"]     = kickoff_brt.astimezone(timezone.utc)
                        game["kickoff_tbd"] = False
                    elif cbf["date_brt"] and not cbf["time_brt"]:
                        year, month, day = map(int, cbf["date_brt"].split("-"))
                        game["kickoff"]     = datetime(year, month, day, tzinfo=timezone.utc)
                        game["kickoff_tbd"] = True
                    else:
                        game["kickoff_tbd"] = True

                    if game["kickoff"] > now_utc or game["kickoff_tbd"]:
                        all_fixtures.append((comp, game))

                # Rounds not yet in CBF PDF → mark TBD
                for g in espn_games:
                    if g["id"] not in added:
                        g["kickoff_tbd"] = True
                        all_fixtures.append((comp, g))

            else:
                # ── FALLBACK: CBF unavailable — use ESPN times directly ────────
                print("    ⚠️  Using ESPN as fallback for Brasileirão times (CBF unavailable)")
                for g in espn_games:
                    # ESPN placeholder times (18:00Z = 15:00 BRT) → all-day
                    if g.get("kickoff_tbd"):
                        g["kickoff_tbd"] = True
                    all_fixtures.append((comp, g))

        else:
            # ── Libertadores / Copa do Brasil: use ESPN ───────────────────────
            games = fetch_espn_scoreboard(comp["league"])
            print(f"    → {len(games)} upcoming games")
            for g in games:
                all_fixtures.append((comp, g))

    # Sort by kickoff, dedup
    seen, unique = set(), []
    for comp, g in sorted(all_fixtures, key=lambda x: x[1]["kickoff"]):
        if g["id"] not in seen:
            seen.add(g["id"])
            unique.append((comp, g))

    return unique

# ── ICS builder ───────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (text
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n"))

def _dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")

def _date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")

def build_ics(fixtures: list[tuple[dict, dict]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Cruzeiro Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:🦊 Cruzeiro Esporte Clube",
        "X-WR-TIMEZONE:America/Chicago",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for comp, f in fixtures:
        kickoff = f["kickoff"]
        is_tbd  = f.get("kickoff_tbd", False)
        uid     = f"cruzeiro-{f['id']}-{comp['league']}@local"

        title = (
            f"⚽️ 🦊 Cruzeiro vs {f['opponent']}"
            if f["is_home"]
            else f"⚽️ 🦊 Cruzeiro @ {f['opponent']}"
        )

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{_dt(datetime.now(timezone.utc))}",
        ]

        if is_tbd:
            # All-day event — no assumed time
            next_day = kickoff + timedelta(days=1)
            lines += [
                f"DTSTART;VALUE=DATE:{_date(kickoff)}",
                f"DTEND;VALUE=DATE:{_date(next_day)}",
            ]
        else:
            end = kickoff + timedelta(hours=2)
            lines += [
                f"DTSTART:{_dt(kickoff)}",
                f"DTEND:{_dt(end)}",
            ]

        lines += [
            f"SUMMARY:{_esc(title)}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

# ── World Cup 2026 ────────────────────────────────────────────────────────────

WC_FLAGS = {
    "Algeria":"🇩🇿","Argentina":"🇦🇷","Australia":"🇦🇺","Austria":"🇦🇹",
    "Belgium":"🇧🇪","Bosnia-Herzegovina":"🇧🇦","Brazil":"🇧🇷","Canada":"🇨🇦",
    "Cape Verde":"🇨🇻","Colombia":"🇨🇴","Congo DR":"🇨🇩","Croatia":"🇭🇷",
    "Curacao":"🇨🇼","Czechia":"🇨🇿","Ecuador":"🇪🇨","Egypt":"🇪🇬",
    "England":"🏴󠁧󠁢󠁥󠁮󠁧󠁿","France":"🇫🇷","Germany":"🇩🇪","Ghana":"🇬🇭",
    "Haiti":"🇭🇹","Iran":"🇮🇷","Iraq":"🇮🇶","Ivory Coast":"🇨🇮",
    "Japan":"🇯🇵","Jordan":"🇯🇴","Mexico":"🇲🇽","Morocco":"🇲🇦",
    "Netherlands":"🇳🇱","New Zealand":"🇳🇿","Norway":"🇳🇴","Panama":"🇵🇦",
    "Paraguay":"🇵🇾","Portugal":"🇵🇹","Qatar":"🇶🇦","Saudi Arabia":"🇸🇦",
    "Scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿","Senegal":"🇸🇳","South Africa":"🇿🇦","South Korea":"🇰🇷",
    "Spain":"🇪🇸","Sweden":"🇸🇪","Switzerland":"🇨🇭","Tunisia":"🇹🇳",
    "Türkiye":"🇹🇷","United States":"🇺🇸","Uruguay":"🇺🇾","Uzbekistan":"🇺🇿",
}

def _wc_is_placeholder(name: str) -> bool:
    """True if the team name is a bracket placeholder (not yet determined)."""
    return any(k in name for k in ("Winner", "Loser", "Place", "Round of", "Quarterfinal", "Semifinal", "Group "))

def _wc_round_label(home: str, away: str, dt: datetime) -> str:
    """Derive a human-readable round name from placeholder team names and date."""
    combined = home + " " + away
    if "Semifinal" in combined and "Loser" in combined:
        return "3rd Place"
    if "Semifinal" in combined:
        return "Final"
    if "Quarterfinal" in combined:
        return "Semifinal"
    if "Round of 16" in combined:
        return "Quarterfinal"
    if "Round of 32" in combined:
        return "Round of 16"
    # Jun 28 – Jul 3 bracket slots (group winners / runners-up / 3rd-place)
    return "Round of 32"

def fetch_world_cup() -> List[Dict]:
    """
    Fetch all 104 FIFA World Cup 2026 games.
    Group stage: real team names + flags.
    Knockout rounds: 🏆 {Round} · TBD vs TBD (updated each run as bracket fills).
    All events: availability=free, 2-hour duration, tagged wc2026 for deletion.
    """
    now_utc = datetime.now(timezone.utc)
    data = _fetch_json(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
        "?dates=20260611-20260719&limit=200"
    )
    if not data:
        print("  ⚠️  World Cup: could not fetch schedule", file=sys.stderr)
        return []

    events   = sorted(data.get("events", []), key=lambda e: e.get("date", ""))
    results  = []

    for ev in events:
        date_raw = ev.get("date", "")
        if not date_raw:
            continue
        try:
            kickoff = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        comps = ev.get("competitions", [{}])[0]
        teams = comps.get("competitors", [])
        home  = next((c for c in teams if c.get("homeAway") == "home"), None)
        away  = next((c for c in teams if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        h_name = home.get("team", {}).get("displayName", "?")
        a_name = away.get("team", {}).get("displayName", "?")

        if _wc_is_placeholder(h_name) or _wc_is_placeholder(a_name):
            round_label = _wc_round_label(h_name, a_name, kickoff)
            title = f"🏆 World Cup {round_label} · TBD vs TBD"
        else:
            h_flag = WC_FLAGS.get(h_name, "🏳️")
            a_flag = WC_FLAGS.get(a_name, "🏳️")
            title  = f"{a_flag} {a_name} vs {h_flag} {h_name}"

        results.append({
            "title":        title,
            "start_iso":    kickoff.isoformat(),
            "end_iso":      (kickoff + timedelta(hours=2)).isoformat(),
            "is_allday":    False,
            "availability": "free",
            "calendar_tag": "wc2026",
        })

    print(f"  ✅ {len(results)} World Cup games fetched "
          f"({sum(1 for r in results if 'TBD' not in r['title'])} group stage · "
          f"{sum(1 for r in results if 'TBD' in r['title'])} knockout)")
    return results


# ── Tennis: João Fonseca upcoming ATP matches ─────────────────────────────────

def _fetch_json(url: str) -> Optional[Dict]:
    """Fetch JSON from url, return None on any error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_fonseca_tennis() -> List[Dict]:
    """
    Fetch João Fonseca's upcoming ATP matches directly from his 2026 season eventlog.
    Returns a list of calendar-payload dicts ready for the Swift binary.
    All tennis events are marked availability=free (times are best estimates).
    """
    now_utc = datetime.now(timezone.utc)
    year    = now_utc.year

    # 1. Fonseca's season eventlog — all his matches for the current ATP season,
    #    each entry has a direct competition $ref we can resolve in parallel.
    eventlog_url = (
        f"{ATP_CORE_BASE}/seasons/{year}/athletes/{FONSECA_ESPN_ID}"
        f"/eventlog?lang=en&region=us&limit=100"
    )
    el = _fetch_json(eventlog_url)
    if not el:
        print("  ⚠️  Tennis: could not fetch Fonseca eventlog", file=sys.stderr)
        return []

    items = el.get("events", {}).get("items", [])
    if not items:
        print("  ℹ️  Tennis: Fonseca eventlog is empty for this season")
        return []

    # 2. Parallel-fetch all competition refs
    comp_refs = [item.get("competition", {}).get("$ref", "") for item in items if item.get("competition", {}).get("$ref")]

    def get_comp(ref):
        return _fetch_json(ref)

    comps = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for comp in ex.map(get_comp, comp_refs):
            if comp:
                comps.append(comp)

    # 3. Filter: upcoming + singles only
    results = []
    for comp in comps:
        date_str = comp.get("date", "")
        if not date_str:
            continue
        try:
            match_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Keep events up to 3 hours after start so the calendar entry
        # stays visible while the match is live
        if match_dt < now_utc - timedelta(hours=3):
            continue

        # Singles only (excludes doubles which show "Player/Partner" names)
        if comp.get("type", {}).get("slug") != "mens-singles":
            continue

        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue

        opponent = next((c for c in competitors if c.get("id") != FONSECA_ESPN_ID), None)
        if not opponent:
            continue

        opp_name = opponent.get("name", "Unknown")
        opp_last = opp_name.split()[-1]
        title    = f"🎾 João Fonseca vs {opp_last}"

        results.append({
            "title":        title,
            "start_iso":    match_dt.isoformat(),
            "end_iso":      (match_dt + timedelta(hours=3)).isoformat(),
            "is_allday":    False,
            "availability": "free",
        })
        print(f"    → {title}  [{match_dt.strftime('%Y-%m-%d %H:%M UTC')}]")

    results.sort(key=lambda x: x["start_iso"])
    if not results:
        print("  ℹ️  No upcoming Fonseca matches scheduled yet")
    return results


# ── FURIA CS2 upcoming matches ────────────────────────────────────────────────

LIQUIPEDIA_API = "https://liquipedia.net/counterstrike/api.php"
FURIA_LP_PAGE  = "FURIA"

def _lp_fetch(page: str) -> str:
    """Fetch parsed HTML from Liquipedia for a given wiki page. Returns empty string on error."""
    url = (f"{LIQUIPEDIA_API}?action=parse&page={urllib.request.quote(page)}"
           f"&prop=text&format=json")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Encoding": "gzip",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            import gzip as _gzip
            try:
                body = _gzip.decompress(raw)
            except Exception:
                body = raw
            return json.loads(body).get("parse", {}).get("text", {}).get("*", "")
    except Exception:
        return ""


def _lp_upcoming_tournament_pages() -> List[str]:
    """Return Liquipedia page paths for FURIA's upcoming CS2 tournaments."""
    html = _lp_fetch(FURIA_LP_PAGE)
    if not html:
        return []

    # The "Upcoming Tournaments" block contains links like /counterstrike/BLAST/Rivals/...
    idx = html.find("Upcoming Tournaments")
    if idx < 0:
        return []
    section = html[idx: idx + 6000]

    pages = []
    seen  = set()
    for m in re.finditer(r'<a href="/counterstrike/([^"#]+)"', section):
        path = m.group(1)
        # Skip meta-links (Category, S-Tier, etc.)
        if any(skip in path for skip in ("Category", "Tier", "Tournaments", "http")):
            continue
        if path not in seen:
            seen.add(path)
            pages.append(path)
    return pages


def fetch_furia_cs2() -> List[Dict]:
    """
    Scrape Liquipedia for confirmed upcoming FURIA CS2 matches (specific time + opponent known).
    Returns calendar-payload dicts. Availability = free (CS2 match times shift occasionally).
    """
    import gzip as _gzip
    now_ts = int(datetime.now(timezone.utc).timestamp())

    tourney_pages = _lp_upcoming_tournament_pages()
    if not tourney_pages:
        print("  ⚠️  FURIA: could not find upcoming tournament pages", file=sys.stderr)
        return []

    print(f"  🎮 Scanning {len(tourney_pages)} tournament(s) for confirmed FURIA matches...")

    results: List[Dict] = []
    seen_ts: set = set()

    for page in tourney_pages:
        html = _lp_fetch(page)
        if not html:
            continue

        # Tournament short name from page path (last segment, prettified)
        tourney_name = page.split("/")[-1].replace("_", " ")

        # Find every timestamp that has FURIA in its local context
        for m in re.finditer(r'data-timestamp="(\d+)"', html):
            ts = int(m.group(1))
            # Keep events up to 4 hours after start (CS2 Bo3 can run long)
            if ts < now_ts - 4 * 3600 or ts in seen_ts:
                continue

            chunk = html[max(0, m.start() - 100): m.start() + 2500]
            if "FURIA" not in chunk:
                continue

            # Extract team names — prefer the <a title="..."> inside .name spans
            # which gives the full team name, not the abbreviation
            team_entries = re.findall(
                r'class="name"[^>]*>\s*<a[^>]*title="([^"]+)"[^>]*>([^<]+)</a>',
                chunk
            )
            full_names = [title for title, _ in team_entries if title]
            short_names = [text for _, text in team_entries if text]

            if not full_names and not short_names:
                continue

            # Opponent = the non-FURIA team
            opponent = None
            for full, short in zip(full_names, short_names):
                if full not in ("FURIA", "FURIA Esports") and short not in ("FURIA",):
                    opponent = full  # prefer full name
                    break

            # Skip if opponent is still unknown / TBD
            if not opponent or opponent.upper() in ("TBD", "BYE", ""):
                continue

            # Best-of format
            bo_m = re.search(r'Bo(\d)', chunk, re.IGNORECASE)
            bo   = f" (Bo{bo_m.group(1)})" if bo_m else ""

            match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            title    = f"🎮 FURIA vs {opponent}"

            results.append({
                "title":        title,
                "start_iso":    match_dt.isoformat(),
                "end_iso":      (match_dt + timedelta(hours=2)).isoformat(),
                "is_allday":    False,
                "availability": "free",
                "calendar_tag": "furia-cs2",
            })
            seen_ts.add(ts)

            BRT = timezone(timedelta(hours=-3))
            print(f"    → {title}{bo}  [{tourney_name}]  "
                  f"[{match_dt.astimezone(BRT).strftime('%a %b %d %H:%M BRT')}]")

    results.sort(key=lambda x: x["start_iso"])
    if not results:
        print("  ℹ️  No confirmed FURIA matches scheduled yet")
    return results


# ── Calendar change tracker ───────────────────────────────────────────────────

CALENDAR_STATE_FILE   = Path.home() / "Library/Application Support/CruzeiroCalendar/calendar_state.json"
CALENDAR_CHANGES_FILE = Path.home() / "Library/Application Support/CruzeiroCalendar/calendar_changes.json"
CHANGELOG_HTML        = Path.home() / "Library/Application Support/CruzeiroCalendar/calendar_changelog.html"

def _sport_tag(title: str) -> str:
    if title.startswith("⚽️"):  return "soccer"
    if title.startswith("🎾"):  return "tennis"
    if title.startswith("🎮"):  return "cs2"
    return "wc"

def _fmt_dt(iso: str, is_allday: bool = False) -> str:
    """Format an ISO datetime as a human-readable BRT string."""
    if is_allday:
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%b %d · TBD")
        except Exception:
            return "TBD"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT).strftime("%a %b %d · %H:%M BRT")
    except Exception:
        return iso

def _sport_scope(payload: list) -> set:
    """Return the set of sport tags present in this payload."""
    return {_sport_tag(ev["title"]) for ev in payload}


def diff_and_log_changes(new_payload: list) -> None:
    """
    Compare new_payload against the saved snapshot for the same sports.
    Logs additions, removals, reschedules, and TBD→confirmed events.
    Regenerates calendar_changelog.html automatically.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    now_utc = datetime.now(timezone.utc)
    scope   = _sport_scope(new_payload)
    changes: List[dict] = []

    # ── Load full persisted state ──────────────────────────────────
    full_state: Dict[str, dict] = {}
    if not CALENDAR_STATE_FILE.exists():
        # First ever run — just save state, nothing to diff
        CALENDAR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALENDAR_STATE_FILE.write_text(
            json.dumps(new_payload, ensure_ascii=False, indent=2)
        )
        _generate_changelog_html()
        return

    try:
        for ev in json.loads(CALENDAR_STATE_FILE.read_text()):
            full_state[ev["title"]] = ev
    except Exception:
        pass

    # Only compare events for the sports being updated this run
    old_events = {t: ev for t, ev in full_state.items() if _sport_tag(t) in scope}
    new_events = {ev["title"]: ev for ev in new_payload}

    # ── Additions and changes ──────────────────────────────────────
    for title, new_ev in new_events.items():
        if title not in old_events:
            changes.append({
                "ts":        now_str,
                "type":      "added",
                "sport":     _sport_tag(title),
                "title":     title,
                "new_start": new_ev["start_iso"],
                "is_allday": new_ev.get("is_allday", False),
            })
        else:
            old_ev    = old_events[title]
            old_tbd   = old_ev.get("is_allday", False)
            new_tbd   = new_ev.get("is_allday", False)
            old_start = old_ev["start_iso"]
            new_start = new_ev["start_iso"]

            if old_tbd and not new_tbd:
                # TBD all-day → real confirmed kickoff
                changes.append({
                    "ts":          now_str,
                    "type":        "confirmed",
                    "sport":       _sport_tag(title),
                    "title":       title,
                    "old_start":   old_start,
                    "old_is_allday": True,
                    "new_start":   new_start,
                })
            elif not old_tbd and old_start != new_start:
                # Real time was adjusted
                changes.append({
                    "ts":        now_str,
                    "type":      "rescheduled",
                    "sport":     _sport_tag(title),
                    "title":     title,
                    "old_start": old_start,
                    "new_start": new_start,
                })

    # ── Removals (future events that disappeared) ──────────────────
    for title, old_ev in old_events.items():
        if title in new_events:
            continue
        try:
            start = datetime.fromisoformat(old_ev["start_iso"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start > now_utc:
                changes.append({
                    "ts":        now_str,
                    "type":      "removed",
                    "sport":     _sport_tag(title),
                    "title":     title,
                    "old_start": old_ev["start_iso"],
                })
        except Exception:
            pass

    # ── Persist changes ────────────────────────────────────────────
    if changes:
        existing: List[dict] = []
        if CALENDAR_CHANGES_FILE.exists():
            try:
                existing = json.loads(CALENDAR_CHANGES_FILE.read_text())
            except Exception:
                pass
        existing.extend(changes)
        CALENDAR_CHANGES_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALENDAR_CHANGES_FILE.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2)
        )
        icons = {"added": "🆕", "confirmed": "✅", "rescheduled": "📅", "removed": "❌"}
        print(f"  📝 {len(changes)} calendar change(s) logged")
        for c in changes:
            print(f"     {icons.get(c['type'], '•')} {c['type']:12}  {c['title']}")

    # ── Merge back into full state (scoped sports replaced) ────────
    for title in [t for t in list(full_state.keys()) if _sport_tag(t) in scope]:
        del full_state[title]
    for title, ev in new_events.items():
        full_state[title] = ev
    CALENDAR_STATE_FILE.write_text(
        json.dumps(list(full_state.values()), ensure_ascii=False, indent=2)
    )

    # ── Regenerate HTML ────────────────────────────────────────────
    _generate_changelog_html()


def _generate_changelog_html() -> None:
    """Read calendar_changes.json and write a beautiful changelog HTML page."""
    from collections import defaultdict

    all_changes: List[dict] = []
    if CALENDAR_CHANGES_FILE.exists():
        try:
            all_changes = json.loads(CALENDAR_CHANGES_FILE.read_text())
        except Exception:
            pass

    all_changes = list(reversed(all_changes))  # newest first

    sport_emoji = {"soccer": "⚽️", "tennis": "🎾", "wc": "🏆", "cs2": "🎮"}
    sport_label = {"soccer": "Cruzeiro", "tennis": "Fonseca", "wc": "World Cup", "cs2": "FURIA"}
    type_icon   = {"added": "🆕", "confirmed": "✅", "rescheduled": "📅", "removed": "❌"}
    type_label  = {"added": "Added", "confirmed": "Time confirmed", "rescheduled": "Rescheduled", "removed": "Removed"}
    type_color  = {"added": "green", "confirmed": "blue", "rescheduled": "orange", "removed": "red"}

    def entry_html(c: dict) -> str:
        sport = c.get("sport", "soccer")
        ctype = c.get("type", "added")
        title = c.get("title", "")
        ts_str = ""
        try:
            dt = datetime.fromisoformat(c["ts"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts_str = dt.astimezone(BRT).strftime("%H:%M")
        except Exception:
            pass

        detail = ""
        if ctype == "added":
            t = _fmt_dt(c.get("new_start", ""), c.get("is_allday", False))
            detail = f'<div class="change-detail"><span class="dl">Scheduled</span><span class="dv">{t}</span></div>'
        elif ctype == "confirmed":
            old = _fmt_dt(c.get("old_start", ""), True)
            new = _fmt_dt(c.get("new_start", ""), False)
            detail = (f'<div class="change-detail">'
                      f'<span class="dl">Was</span>'
                      f'<span class="dv old">{old}</span>'
                      f'<span class="arr">→</span>'
                      f'<span class="dv new">{new}</span>'
                      f'</div>')
        elif ctype == "rescheduled":
            old = _fmt_dt(c.get("old_start", ""), False)
            new = _fmt_dt(c.get("new_start", ""), False)
            detail = (f'<div class="change-detail">'
                      f'<span class="dl">Was</span>'
                      f'<span class="dv old">{old}</span>'
                      f'<span class="arr">→</span>'
                      f'<span class="dv new">{new}</span>'
                      f'</div>')
        elif ctype == "removed":
            t = _fmt_dt(c.get("old_start", ""), False)
            detail = f'<div class="change-detail"><span class="dl">Was</span><span class="dv old">{t}</span></div>'

        return (
            f'<div class="entry e-{type_color[ctype]}">'
            f'<div class="el"><div class="ti">{type_icon[ctype]}</div><div class="et">{ts_str}</div></div>'
            f'<div class="eb"><div class="etitle">{title}</div>{detail}</div>'
            f'<div class="er">'
            f'<span class="badge b-{sport}">{sport_emoji.get(sport,"")}&nbsp;{sport_label.get(sport,"")}</span>'
            f'<span class="badge bt-{type_color[ctype]}">{type_label[ctype]}</span>'
            f'</div></div>'
        )

    def date_label(date_key: str) -> str:
        try:
            dt   = datetime.strptime(date_key, "%Y-%m-%d")
            today = datetime.now(BRT).date()
            d     = dt.date()
            if d == today:                    return "Today"
            if d == today - timedelta(days=1): return "Yesterday"
            return dt.strftime("%A, %b %d")
        except Exception:
            return date_key

    by_date: Dict[str, list] = defaultdict(list)
    for c in all_changes:
        try:
            dt = datetime.fromisoformat(c["ts"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dk = dt.astimezone(BRT).strftime("%Y-%m-%d")
        except Exception:
            dk = "Unknown"
        by_date[dk].append(c)

    total  = len(all_changes)
    counts = {t: sum(1 for c in all_changes if c.get("type") == t)
              for t in ("added", "confirmed", "rescheduled", "removed")}

    sections = ""
    for dk in sorted(by_date.keys(), reverse=True):
        entries = "".join(entry_html(c) for c in by_date[dk])
        sections += f'<div class="dg"><div class="dlabel">{date_label(dk)}</div>{entries}</div>'

    updated = datetime.now(timezone.utc).astimezone(BRT).strftime("%b %d, %Y · %H:%M BRT")
    empty   = '<div class="empty">No changes recorded yet.</div>' if not all_changes else sections

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Calendar Activity</title>
<style>
:root{{--bg:#0d0f14;--surf:#161a23;--bdr:#232736;--muted:#3a3f52;--txt:#e2e6f3;--sub:#7b82a0;
      --green:#3ecf72;--blue:#5b7cfa;--org:#f5a623;--red:#f25f5c;--pur:#b48efa;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
     font-size:14px;line-height:1.6;padding:48px 24px 80px}}
.hdr{{max-width:720px;margin:0 auto 36px}}
.hdr h1{{font-size:24px;font-weight:700;letter-spacing:-.4px}}
.hdr p{{color:var(--sub);font-size:12px;margin-top:5px}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}}
.stat{{background:var(--surf);border:1px solid var(--bdr);border-radius:10px;padding:12px 18px;min-width:100px}}
.sv{{font-size:22px;font-weight:700}}.sl{{font-size:11px;color:var(--sub);margin-top:2px}}
.sg .sv{{color:var(--sub)}}.sgreen .sv{{color:var(--green)}}.sblue .sv{{color:var(--blue)}}
.sorg .sv{{color:var(--org)}}.sred .sv{{color:var(--red)}}
.main{{max-width:720px;margin:0 auto}}
.dg{{margin-bottom:28px}}
.dlabel{{font-size:11px;font-weight:600;letter-spacing:.7px;text-transform:uppercase;
         color:var(--sub);padding-bottom:8px;border-bottom:1px solid var(--bdr);margin-bottom:10px}}
.entry{{display:flex;align-items:flex-start;gap:14px;background:var(--surf);
        border:1px solid var(--bdr);border-left:3px solid transparent;
        border-radius:10px;padding:13px 15px;margin-bottom:8px;transition:border-color .15s}}
.entry:hover{{border-color:var(--muted)}}
.e-green{{border-left-color:var(--green)}}.e-blue{{border-left-color:var(--blue)}}
.e-orange{{border-left-color:var(--org)}}.e-red{{border-left-color:var(--red)}}
.el{{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:34px}}
.ti{{font-size:17px;line-height:1}}.et{{font-size:10px;color:var(--sub);white-space:nowrap}}
.eb{{flex:1;min-width:0}}
.etitle{{font-size:14px;font-weight:600}}
.change-detail{{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:5px}}
.dl{{font-size:11px;font-weight:500;color:var(--sub);background:var(--bdr);border-radius:4px;padding:1px 6px}}
.dv{{font-size:12px;color:var(--sub)}}.old{{text-decoration:line-through;opacity:.55}}
.new{{color:var(--txt);font-weight:500}}.arr{{color:var(--sub);font-size:11px}}
.er{{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}}
.badge{{font-size:10.5px;font-weight:600;padding:2px 9px;border-radius:999px;
        border:1px solid transparent;white-space:nowrap}}
.b-soccer{{color:var(--green);border-color:rgba(62,207,114,.3);background:rgba(62,207,114,.08)}}
.b-tennis{{color:var(--org);border-color:rgba(245,166,35,.3);background:rgba(245,166,35,.08)}}
.b-wc{{color:var(--blue);border-color:rgba(91,124,250,.3);background:rgba(91,124,250,.08)}}
.b-cs2{{color:var(--pur);border-color:rgba(180,142,250,.3);background:rgba(180,142,250,.08)}}
.bt-green{{color:var(--green);border-color:rgba(62,207,114,.3);background:rgba(62,207,114,.08)}}
.bt-blue{{color:var(--blue);border-color:rgba(91,124,250,.3);background:rgba(91,124,250,.08)}}
.bt-orange{{color:var(--org);border-color:rgba(245,166,35,.3);background:rgba(245,166,35,.08)}}
.bt-red{{color:var(--red);border-color:rgba(242,95,92,.3);background:rgba(242,95,92,.08)}}
.empty{{text-align:center;color:var(--sub);padding:60px 0;font-size:13px}}
.footer{{text-align:center;color:var(--muted);font-size:11px;margin-top:48px}}
</style>
</head>
<body>
<div class="hdr">
  <h1>📋 Calendar Activity</h1>
  <p>Every change to your Sports on TV calendar — reschedules, confirmations, new games, and removals.</p>
  <div class="stats">
    <div class="stat sg"><div class="sv">{total}</div><div class="sl">All changes</div></div>
    <div class="stat sgreen"><div class="sv">{counts['added']}</div><div class="sl">🆕 Added</div></div>
    <div class="stat sblue"><div class="sv">{counts['confirmed']}</div><div class="sl">✅ Confirmed</div></div>
    <div class="stat sorg"><div class="sv">{counts['rescheduled']}</div><div class="sl">📅 Rescheduled</div></div>
    <div class="stat sred"><div class="sv">{counts['removed']}</div><div class="sl">❌ Removed</div></div>
  </div>
</div>
<div class="main">{empty}</div>
<div class="footer">Updated {updated}</div>
</body>
</html>"""

    CHANGELOG_HTML.write_text(html, encoding="utf-8")


# ── Apple Calendar writer (real events via AppleScript) ───────────────────────

def write_to_apple_calendar(
    fixtures,
    tennis_events=None,
    wc_events=None,
    cs2_events=None,
) -> bool:
    """
    Write all fixtures + tennis events to 'Sports on TV' via EventKit (Swift helper).
    Talks directly to CalendarAgent — Calendar.app never opens.
    Deletes existing ⚽️ 🦊 Cruzeiro and 🎾 João Fonseca events before re-adding.
    """
    import subprocess, json as _json

    SWIFT_BINARY = Path.home() / "Library/Application Support/CruzeiroCalendar/update_calendar"
    JSON_TMP     = Path("/tmp/cruzeiro_fixtures.json")

    # Build the JSON payload — soccer fixtures first
    payload = []
    for comp, f in fixtures:
        kickoff = f["kickoff"]
        is_tbd  = f.get("kickoff_tbd", False)

        title = (
            f"⚽️ 🦊 Cruzeiro vs {f['opponent']}"
            if f["is_home"]
            else f"⚽️ 🦊 Cruzeiro @ {f['opponent']}"
        )

        if is_tbd:
            brt_day = kickoff.astimezone(BRT)
            start   = datetime(brt_day.year, brt_day.month, brt_day.day, tzinfo=timezone.utc)
            end     = start + timedelta(days=1)
        else:
            start = kickoff
            end   = kickoff + timedelta(hours=2)

        payload.append({
            "title":     title,
            "start_iso": start.isoformat(),
            "end_iso":   end.isoformat(),
            "is_allday": is_tbd,
            # Soccer: TBD → free (no confirmed time), confirmed → busy
            "availability": "free" if is_tbd else "busy",
        })

    # Append tennis events (already fully formed payload dicts)
    for t in (tennis_events or []):
        payload.append(t)

    # Append World Cup events
    for w in (wc_events or []):
        payload.append(w)

    # Append FURIA CS2 events
    for c in (cs2_events or []):
        payload.append(c)

    # Build meta: tell Swift exactly which prefixes/tags to delete
    # so a fast run (tennis+FURIA only) never touches soccer or WC events.
    delete_prefixes = []
    delete_tags     = []
    if fixtures:
        delete_prefixes.append("⚽️ 🦊 Cruzeiro")
    if tennis_events is not None:
        delete_prefixes.append("🎾 João Fonseca")
    if wc_events is not None:
        delete_tags.append("wc2026")
    if cs2_events is not None:
        delete_tags.append("furia-cs2")

    # Diff against last snapshot and log any changes
    diff_and_log_changes(payload)

    envelope = {
        "meta":   {"delete_prefixes": delete_prefixes, "delete_tags": delete_tags},
        "events": payload,
    }

    JSON_TMP.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    print("  Running EventKit helper (no Calendar.app)…")
    result = subprocess.run(
        [str(SWIFT_BINARY), str(JSON_TMP)],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"  ⚠️  EventKit error: {result.stderr.strip()}", file=sys.stderr)
        return False

    print(f"  {result.stdout.strip()}")
    return True


# ── Dashboard auto-update ────────────────────────────────────────────────────

def _update_dashboards(cruzeiro: bool = False, fonseca: bool = False, furia: bool = False) -> None:
    """Regenerate HTML dashboards. Each flag is independent; failures are silent."""
    parent = str(Path(__file__).parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    if cruzeiro:
        try:
            from cruzeiro_dashboard import build_cruzeiro_html
            print("\n  📊 Updating Cruzeiro dashboard...")
            build_cruzeiro_html()
        except Exception as e:
            print(f"  ⚠️  Cruzeiro dashboard: {e}", file=sys.stderr)

    if fonseca:
        try:
            from fonseca_dashboard import build_fonseca_html
            print("\n  📊 Updating Fonseca dashboard...")
            build_fonseca_html()
        except Exception as e:
            print(f"  ⚠️  Fonseca dashboard: {e}", file=sys.stderr)

    if furia:
        try:
            from furia_dashboard import build_furia_html
            print("\n  📊 Updating FURIA dashboard...")
            build_furia_html()
        except Exception as e:
            print(f"  ⚠️  FURIA dashboard: {e}", file=sys.stderr)


# ── Fast update (tennis + FURIA only, runs every 20 min near game time) ───────

DAILY_STATE_FILE = Path.home() / "Library/Application Support/CruzeiroCalendar/last_tennis_furia_daily.txt"

def fast_update():
    """
    Runs every 5 minutes. Two behaviors:

    • Game TODAY or TOMORROW → always fetch + update (times shift up to match start)
    • No game soon           → fetch + update once per day, then skip the rest of the ticks
                               (picks up newly announced matches without hammering the APIs)

    Soccer and World Cup events are never touched.
    """
    now_utc    = datetime.now(timezone.utc)
    now_brt    = now_utc.astimezone(BRT)
    today      = now_brt.date()
    tomorrow   = today + timedelta(days=1)

    print(f"\n⚡ Fast update — {now_brt.strftime('%Y-%m-%d %H:%M BRT')}")

    tennis_events = fetch_fonseca_tennis()
    cs2_events    = fetch_furia_cs2()

    all_events = tennis_events + cs2_events

    # Check if any match is today or tomorrow (BRT date)
    game_soon = False
    for ev in all_events:
        try:
            ev_date = datetime.fromisoformat(ev["start_iso"]).astimezone(BRT).date()
            if ev_date in (today, tomorrow):
                game_soon = True
                break
        except Exception:
            pass

    if game_soon:
        print(f"  🔔 Game today or tomorrow — refreshing calendar…")
        for ev in all_events:
            try:
                ev_date = datetime.fromisoformat(ev["start_iso"]).astimezone(BRT).date()
                if ev_date in (today, tomorrow):
                    dt_str = datetime.fromisoformat(ev["start_iso"]).astimezone(BRT).strftime("%a %b %d · %H:%M BRT")
                    print(f"     {ev['title']}  [{dt_str}]")
            except Exception:
                pass
        write_to_apple_calendar(
            fixtures      = [],
            tennis_events = tennis_events,
            wc_events     = None,
            cs2_events    = cs2_events,
        )
        return

    # No game today or tomorrow — only update once per day
    last_daily = DAILY_STATE_FILE.read_text().strip() if DAILY_STATE_FILE.exists() else ""
    if last_daily == str(today):
        print(f"  ℹ️  No game today/tomorrow · daily sync already done — skipping.")
        return

    print(f"  📅 Daily sync — updating tennis + FURIA schedule…")
    if all_events:
        for ev in all_events:
            try:
                dt_str = datetime.fromisoformat(ev["start_iso"]).astimezone(BRT).strftime("%a %b %d · %H:%M BRT")
                print(f"     {ev['title']}  [{dt_str}]")
            except Exception:
                pass
    write_to_apple_calendar(
        fixtures      = [],
        tennis_events = tennis_events,
        wc_events     = None,
        cs2_events    = cs2_events,
    )
    DAILY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAILY_STATE_FILE.write_text(str(today))
    _update_dashboards(fonseca=True, furia=True)


def soccer_update():
    """
    Weekly: refresh Cruzeiro fixtures only (CBF PDF + ESPN).
    Never touches tennis, FURIA, or World Cup events.
    """
    print(f"\n⚽️  Soccer update — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    print("\n[1/2] Fetching CBF official Brasileirão schedule...")
    cbf_schedule = get_cbf_schedule()

    print("\n[2/2] Fetching all Cruzeiro fixtures...")
    fixtures = fetch_all_fixtures(cbf_schedule)

    tbd_count       = sum(1 for _, f in fixtures if f.get("kickoff_tbd"))
    confirmed_count = len(fixtures) - tbd_count
    print(f"\n  ⚽️  Total: {len(fixtures)} fixtures  ({confirmed_count} confirmed · {tbd_count} TBD/all-day)")

    ics_content = build_ics(fixtures)
    OUTPUT_FILE.write_text(ics_content, encoding="utf-8")
    try:
        OUTPUT_MIRROR.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_MIRROR.write_text(ics_content, encoding="utf-8")
    except Exception as e:
        print(f"⚠️  Mirror copy failed: {e}", file=sys.stderr)

    print("\n  Writing Cruzeiro events to Apple Calendar…")
    write_to_apple_calendar(
        fixtures      = fixtures,
        tennis_events = None,
        wc_events     = None,
        cs2_events    = None,
    )
    _update_dashboards(cruzeiro=True)


def worldcup_update():
    """
    Weekly (active from June 1): refresh World Cup knockout bracket slots.
    Group stage is static — no need to run before June.
    """
    now = datetime.now(timezone.utc)
    if now < datetime(2026, 6, 24, tzinfo=timezone.utc):
        print(f"\n🌍 World Cup update — skipping, knockout bracket not active yet "
              f"(today {now.strftime('%b %d')}, active from Jun 24).")
        return

    print(f"\n🌍 World Cup update — {now.strftime('%Y-%m-%d %H:%M')}")
    wc_events = fetch_world_cup()

    print("\n  Writing World Cup events to Apple Calendar…")
    write_to_apple_calendar(
        fixtures      = [],
        tennis_events = None,
        wc_events     = wc_events,
        cs2_events    = None,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🦊 Cruzeiro · 🎾 Fonseca · 🌍 World Cup · 🎮 FURIA — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    print("\n[1/3] Fetching CBF official Brasileirão schedule...")
    cbf_schedule = get_cbf_schedule()

    print("\n[2/3] Fetching all Cruzeiro fixtures...")
    fixtures = fetch_all_fixtures(cbf_schedule)

    tbd_count       = sum(1 for _, f in fixtures if f.get("kickoff_tbd"))
    confirmed_count = len(fixtures) - tbd_count
    print(f"\n  ⚽️  Total: {len(fixtures)} fixtures  ({confirmed_count} confirmed · {tbd_count} TBD/all-day)")

    ics_content = build_ics(fixtures)
    OUTPUT_FILE.write_text(ics_content, encoding="utf-8")
    print(f"✅ ICS written → {OUTPUT_FILE}")

    # Mirror copy for the local HTTP server (launchd can't read OneDrive/CloudStorage)
    try:
        OUTPUT_MIRROR.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_MIRROR.write_text(ics_content, encoding="utf-8")
        print(f"✅ Mirror copy  written → {OUTPUT_MIRROR}")
    except Exception as e:
        print(f"⚠️  Could not write mirror copy: {e}", file=sys.stderr)

    print("\n[3/4] Fetching João Fonseca ATP matches...")
    tennis_events = fetch_fonseca_tennis()
    print(f"  🎾  Total: {len(tennis_events)} upcoming Fonseca match(es)")

    print("\n[4/5] Fetching FIFA World Cup 2026 schedule...")
    wc_events = fetch_world_cup()

    print("\n[5/5] Fetching FURIA CS2 confirmed matches...")
    cs2_events = fetch_furia_cs2()
    print(f"  🎮  Total: {len(cs2_events)} confirmed FURIA match(es)")

    print("\n  Writing to Apple Calendar (Sports on TV)…")
    write_to_apple_calendar(fixtures, tennis_events, wc_events, cs2_events)
    _update_dashboards(cruzeiro=True, fonseca=True, furia=True)

    if fixtures:
        print("\nUpcoming Cruzeiro schedule:")
        for comp, f in fixtures:
            is_tbd = f.get("kickoff_tbd", False)
            if is_tbd:
                time_str = "TBD (all-day)"
            else:
                ct = f["kickoff"].astimezone(timezone(timedelta(hours=-5)))
                time_str = ct.strftime("%a %b %d · %I:%M %p CT")
            ha    = "🏠" if f["is_home"] else "✈️ "
            title = f"🦊 Cruzeiro vs {f['opponent']}" if f["is_home"] else f"🦊 Cruzeiro @ {f['opponent']}"
            print(f"  {ha} {title:<40} {time_str:<28} [{comp['name']}]")

    if tennis_events:
        print("\nUpcoming Fonseca matches:")
        for t in tennis_events:
            try:
                dt = datetime.fromisoformat(t["start_iso"]).astimezone(timezone(timedelta(hours=-5)))
                time_str = dt.strftime("%a %b %d · %I:%M %p CT")
            except Exception:
                time_str = t["start_iso"]
            print(f"  🎾 {t['title']:<55} {time_str}")

    if cs2_events:
        print("\nUpcoming FURIA matches:")
        for c in cs2_events:
            try:
                dt = datetime.fromisoformat(c["start_iso"]).astimezone(timezone(timedelta(hours=-3)))
                time_str = dt.strftime("%a %b %d · %H:%M BRT")
            except Exception:
                time_str = c["start_iso"]
            print(f"  🎮 {c['title']:<45} {time_str}")


if __name__ == "__main__":
    if "--fast" in sys.argv:
        fast_update()
    elif "--soccer" in sys.argv:
        soccer_update()
    elif "--worldcup" in sys.argv:
        worldcup_update()
    else:
        main()   # manual full run
