#!/usr/bin/env python3
"""
fonseca_dashboard.py
Generates fonseca.html — João Fonseca's 2026 ATP season results.
Run manually or call build_fonseca_html() from another script.
"""

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

ATP_CORE_BASE    = "http://sports.core.api.espn.com/v2/sports/tennis/leagues/atp"
FONSECA_ESPN_ID  = "11745"
OUTPUT_HTML      = Path.home() / "Library/Application Support/CruzeiroCalendar/fonseca.html"

# ── helpers ───────────────────────────────────────────────────────────────────

def _fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _extract_score(note_text: str) -> str:
    """Pull just the set scores from the ESPN notes string."""
    if not note_text:
        return ""
    m = re.search(r'\([A-Z]{2,3}\)\s+([\d\s()\-/w]+)$', note_text.strip())
    return m.group(1).strip() if m else ""


def _tournament_short(name: str) -> str:
    """Shorten long tournament names for display."""
    replacements = {
        "presented by": "", "Open presented by": "Open",
        "Masters Series": "Masters", " by Bitpanda": "",
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    return name.strip()


def _nav(active: str) -> str:
    items = [
        ("/cruzeiro_results.html",   "⚽️ Cruzeiro"),
        ("/fonseca.html",            "🎾 Fonseca"),
        ("/furia_results.html",      "🎮 FURIA"),
        ("/calendar_changelog.html", "📋 Activity"),
        ("/changelog.html",          "🛠 Changelog"),
    ]
    links = "".join(
        f'<a href="{h}" class="nv{" nv-on" if h == active else ""}">{l}</a>'
        for h, l in items
    )
    return (
        '<nav class="topnav">'
        '<a href="/" class="nav-home" title="Hub">🦊</a>'
        f'<div class="nav-links">{links}</div>'
        '</nav>'
        '<style>'
        '.topnav{background:#161a23;border-bottom:1px solid #232736;padding:0 20px;'
        'display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100;min-height:48px}'
        '.nav-home{font-size:20px;text-decoration:none;flex-shrink:0}'
        '.nav-links{display:flex;gap:2px;flex-wrap:wrap}'
        '.nv{font-size:12.5px;font-weight:500;color:#7b82a0;text-decoration:none;'
        'padding:6px 11px;border-radius:6px;white-space:nowrap;transition:background .15s,color .15s}'
        '.nv:hover,.nv-on{background:#232736;color:#e2e6f3}'
        '</style>'
    )


# ── data fetch ────────────────────────────────────────────────────────────────

def fetch_all_matches():
    now_utc = datetime.now(timezone.utc)
    year    = now_utc.year

    el = _fetch(f"{ATP_CORE_BASE}/seasons/{year}/athletes/{FONSECA_ESPN_ID}/eventlog?lang=en&region=us&limit=100")
    if not el:
        return []

    items = el.get("events", {}).get("items", [])

    def get_both(item):
        comp  = _fetch(item["competition"]["$ref"])
        event = _fetch(item["event"]["$ref"])
        return comp, event

    matches = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for comp, event in ex.map(get_both, items):
            if not comp or not event:
                continue

            comp_type = comp.get("type", {}).get("slug", "")
            if comp_type != "mens-singles":
                continue

            fonseca  = next((c for c in comp.get("competitors", []) if c.get("id") == FONSECA_ESPN_ID), None)
            opponent = next((c for c in comp.get("competitors", []) if c.get("id") != FONSECA_ESPN_ID), None)
            if not fonseca or not opponent:
                continue

            dt_str  = comp.get("date", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            notes     = comp.get("notes", [])
            note_text = notes[0].get("text", "") if notes else ""
            score     = _extract_score(note_text)

            opp_name  = opponent.get("name", "?")
            opp_seed_m = re.search(rf'\((\d+)\)\s+{re.escape(opp_name.split()[0])}', note_text)
            opp_seed   = opp_seed_m.group(1) if opp_seed_m else ""

            fon_seed_m = re.search(r'\((\d+)\)\s+Joao\s+Fonseca', note_text)
            fon_seed   = fon_seed_m.group(1) if fon_seed_m else ""

            matches.append({
                "dt":          dt,
                "tournament":  _tournament_short(event.get("shortName") or event.get("name") or "?"),
                "round":       comp.get("round", {}).get("description", "?"),
                "opponent":    opp_name,
                "opp_seed":    opp_seed,
                "fon_seed":    fon_seed,
                "win":         fonseca.get("winner", False),
                "score":       score,
                "upcoming":    dt > now_utc,
                "walkover":    "w/o" in note_text.lower() or "bye" in opp_name.lower(),
            })

    matches.sort(key=lambda x: x["dt"])
    return matches


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_html(matches) -> str:
    now_utc    = datetime.now(timezone.utc)
    year       = now_utc.year
    past       = [m for m in matches if not m["upcoming"]]
    upcoming   = [m for m in matches if m["upcoming"]]
    wins       = sum(1 for m in past if m["win"] and not m["walkover"])
    losses     = sum(1 for m in past if not m["win"] and not m["walkover"])
    walkovers  = sum(1 for m in past if m["walkover"])
    tournaments = len({m["tournament"] for m in past})
    generated  = datetime.now().strftime("%B %d, %Y at %H:%M")

    def match_card(m):
        if m["upcoming"]:
            cls   = "upcoming"
            badge = '<span class="badge upcoming-badge">Upcoming</span>'
            score_html = ""
        elif m["walkover"]:
            cls   = "walkover"
            badge = '<span class="badge walkover-badge">W/O</span>'
            score_html = ""
        elif m["win"]:
            cls   = "win"
            badge = '<span class="badge win-badge">W</span>'
            score_html = f'<div class="score">{m["score"]}</div>' if m["score"] else ""
        else:
            cls   = "loss"
            badge = '<span class="badge loss-badge">L</span>'
            score_html = f'<div class="score">{m["score"]}</div>' if m["score"] else ""

        dt_local = m["dt"].astimezone(timezone(timedelta(hours=-3)))  # BRT
        date_str = dt_local.strftime("%b %d")
        time_str = dt_local.strftime("%H:%M BRT") if not m["upcoming"] or m["dt"].hour != 0 else ""

        fon_seed_html = f'<sup class="seed">({m["fon_seed"]})</sup> ' if m["fon_seed"] else ""
        opp_seed_html = f' <sup class="seed">({m["opp_seed"]})</sup>' if m["opp_seed"] else ""

        return f"""
        <div class="card {cls}">
          <div class="card-left">
            {badge}
            <div class="date">{date_str}</div>
          </div>
          <div class="card-center">
            <div class="matchup">{fon_seed_html}João Fonseca <span class="vs">vs</span> {m['opponent']}{opp_seed_html}</div>
            <div class="meta">{m['tournament']} · {m['round']}</div>
            {score_html}
          </div>
          <div class="card-right">
            {f'<div class="time">{time_str}</div>' if time_str else ''}
          </div>
        </div>"""

    upcoming_section = ""
    if upcoming:
        cards = "\n".join(match_card(m) for m in upcoming)
        upcoming_section = f"""
      <h2 class="section-title">Upcoming</h2>
      {cards}"""

    results_cards = "\n".join(match_card(m) for m in reversed(past))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🎾 João Fonseca — {year} Season</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      min-height: 100vh;
      padding: 32px 16px 64px;
    }}

    /* ── header ── */
    .header {{
      text-align: center;
      margin-bottom: 36px;
    }}
    .header h1 {{
      font-size: 2rem;
      font-weight: 700;
      letter-spacing: -0.5px;
      color: #fff;
    }}
    .header .subtitle {{
      color: #718096;
      margin-top: 4px;
      font-size: .9rem;
    }}

    /* ── stats bar ── */
    .stats {{
      display: flex;
      justify-content: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 36px;
    }}
    .stat {{
      background: #1a1f2e;
      border: 1px solid #2d3748;
      border-radius: 12px;
      padding: 16px 24px;
      text-align: center;
      min-width: 100px;
    }}
    .stat-num {{
      font-size: 2rem;
      font-weight: 700;
      line-height: 1;
    }}
    .stat-label {{
      font-size: .75rem;
      color: #718096;
      margin-top: 4px;
      text-transform: uppercase;
      letter-spacing: .05em;
    }}
    .stat-wins   .stat-num {{ color: #48bb78; }}
    .stat-losses .stat-num {{ color: #fc8181; }}
    .stat-record .stat-num {{ font-size: 1.5rem; color: #e2e8f0; }}

    /* ── section ── */
    .section-title {{
      font-size: .8rem;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #718096;
      margin: 28px 0 12px;
    }}

    /* ── cards ── */
    .container {{ max-width: 760px; margin: 0 auto; }}

    .card {{
      display: flex;
      align-items: center;
      gap: 16px;
      background: #1a1f2e;
      border: 1px solid #2d3748;
      border-radius: 12px;
      padding: 14px 18px;
      margin-bottom: 10px;
      transition: border-color .15s;
    }}
    .card:hover {{ border-color: #4a5568; }}

    .card.win      {{ border-left: 4px solid #48bb78; }}
    .card.loss     {{ border-left: 4px solid #fc8181; }}
    .card.walkover {{ border-left: 4px solid #4a5568; }}
    .card.upcoming {{ border-left: 4px solid #63b3ed; background: #1a2234; }}

    .card-left {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 6px;
      min-width: 52px;
    }}
    .date {{
      font-size: .75rem;
      color: #718096;
      white-space: nowrap;
    }}

    .card-center {{ flex: 1; }}
    .matchup {{
      font-size: 1rem;
      font-weight: 600;
      color: #e2e8f0;
      line-height: 1.3;
    }}
    .vs {{ color: #718096; font-weight: 400; font-size: .9rem; }}
    .meta {{
      font-size: .78rem;
      color: #718096;
      margin-top: 3px;
    }}
    .score {{
      font-size: .82rem;
      color: #a0aec0;
      margin-top: 5px;
      font-variant-numeric: tabular-nums;
    }}

    .card-right {{
      text-align: right;
      min-width: 70px;
    }}
    .time {{
      font-size: .75rem;
      color: #718096;
    }}

    /* ── badges ── */
    .badge {{
      display: inline-block;
      font-size: .65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .05em;
      padding: 2px 7px;
      border-radius: 6px;
    }}
    .win-badge      {{ background: #22543d; color: #68d391; }}
    .loss-badge     {{ background: #742a2a; color: #fc8181; }}
    .walkover-badge {{ background: #2d3748; color: #718096; }}
    .upcoming-badge {{ background: #1a365d; color: #63b3ed; }}

    .seed {{
      font-size: .7rem;
      color: #718096;
      font-weight: 400;
      vertical-align: super;
    }}

    /* ── footer ── */
    .footer {{
      text-align: center;
      margin-top: 48px;
      font-size: .75rem;
      color: #4a5568;
    }}

    @media (max-width: 500px) {{
      .card {{ flex-wrap: wrap; }}
      .card-right {{ display: none; }}
    }}
  </style>
</head>
<body>
{_nav("/fonseca.html")}
  <div class="container">

    <div class="header">
      <h1>🎾 João Fonseca</h1>
      <div class="subtitle">{year} ATP Season</div>
    </div>

    <div class="stats">
      <div class="stat stat-record">
        <div class="stat-num">{wins}–{losses}</div>
        <div class="stat-label">Record</div>
      </div>
      <div class="stat stat-wins">
        <div class="stat-num">{wins}</div>
        <div class="stat-label">Wins</div>
      </div>
      <div class="stat stat-losses">
        <div class="stat-num">{losses}</div>
        <div class="stat-label">Losses</div>
      </div>
      <div class="stat">
        <div class="stat-num">{tournaments}</div>
        <div class="stat-label">Tournaments</div>
      </div>
      <div class="stat">
        <div class="stat-num">{len(upcoming)}</div>
        <div class="stat-label">Upcoming</div>
      </div>
    </div>

    {upcoming_section}

    <h2 class="section-title">Results — most recent first</h2>
    {results_cards}

    <div class="footer">Updated {generated} · source: ESPN ATP API</div>

  </div>
</body>
</html>"""


# ── entry point ───────────────────────────────────────────────────────────────

def build_fonseca_html():
    print("  Fetching Fonseca match history...")
    matches = fetch_all_matches()
    if not matches:
        print("  ⚠️  No match data returned", file=sys.stderr)
        return
    html = build_html(matches)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    past     = sum(1 for m in matches if not m["upcoming"])
    upcoming = sum(1 for m in matches if m["upcoming"])
    print(f"  ✅ Dashboard → {OUTPUT_HTML}  ({past} results · {upcoming} upcoming)")


if __name__ == "__main__":
    build_fonseca_html()
