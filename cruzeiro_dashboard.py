#!/usr/bin/env python3
"""
cruzeiro_dashboard.py
Generates cruzeiro_results.html — Cruzeiro EC's 2026 season results & schedule.
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ESPN_BASE        = "https://site.api.espn.com/apis/site/v2/sports/soccer"
CRUZEIRO_ESPN_ID = "2022"
OUTPUT_HTML      = Path.home() / "Library/Application Support/CruzeiroCalendar/cruzeiro_results.html"
BRT              = timezone(timedelta(hours=-3))

COMPETITIONS = [
    {"league": "bra.1",                "name": "Brasileirão",    "emoji": "🇧🇷", "color": "#3ecf72"},
    {"league": "conmebol.libertadores", "name": "Libertadores",   "emoji": "🌎", "color": "#5b7cfa"},
    {"league": "bra.copa_do_brazil",    "name": "Copa do Brasil", "emoji": "🏆", "color": "#f5a623"},
]


def _fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_cruzeiro_games():
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=120)
    end   = now + timedelta(days=180)
    games, seen = [], set()

    for comp in COMPETITIONS:
        dates = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        data  = _fetch(f"{ESPN_BASE}/{comp['league']}/scoreboard?dates={dates}&limit=200")
        if not data:
            print(f"  ⚠️  No data for {comp['name']}", file=sys.stderr)
            continue

        for event in data.get("events", []):
            if "Cruzeiro" not in event.get("name", ""):
                continue
            eid = event.get("id")
            if eid in seen:
                continue
            seen.add(eid)

            try:
                dt = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except Exception:
                continue

            status_type = event.get("status", {}).get("type", {})
            completed   = status_type.get("completed", False)

            comp_data   = event.get("competitions", [{}])[0]
            competitors = comp_data.get("competitors", [])
            crz = next((c for c in competitors if c.get("id") == CRUZEIRO_ESPN_ID), None)
            opp = next((c for c in competitors if c.get("id") != CRUZEIRO_ESPN_ID), None)
            if not crz or not opp:
                continue

            is_home  = crz.get("homeAway") == "home"
            opp_name = opp.get("team", {}).get("displayName", "?")

            crz_goals = opp_goals = result = None
            if completed:
                try:
                    crz_goals = int(crz.get("score", 0))
                    opp_goals = int(opp.get("score", 0))
                except Exception:
                    crz_goals = opp_goals = 0
                if crz.get("winner"):
                    result = "W"
                elif opp.get("winner"):
                    result = "L"
                else:
                    result = "D"

            notes      = comp_data.get("notes", [])
            round_name = notes[0].get("headline", "") if notes else ""

            games.append({
                "dt":        dt,
                "comp":      comp,
                "is_home":   is_home,
                "opponent":  opp_name,
                "result":    result,
                "crz_goals": crz_goals,
                "opp_goals": opp_goals,
                "completed": completed,
                "round":     round_name,
            })

    games.sort(key=lambda x: x["dt"])
    return games


def build_html(games) -> str:
    now_utc  = datetime.now(timezone.utc)
    past     = [g for g in games if g["completed"]]
    upcoming = [g for g in games if not g["completed"]]

    wins   = sum(1 for g in past if g["result"] == "W")
    draws  = sum(1 for g in past if g["result"] == "D")
    losses = sum(1 for g in past if g["result"] == "L")
    gf     = sum(g["crz_goals"] or 0 for g in past)
    ga     = sum(g["opp_goals"] or 0 for g in past)
    generated = datetime.now(BRT).strftime("%b %d, %Y · %H:%M BRT")

    def game_card(g):
        brt_dt   = g["dt"].astimezone(BRT)
        date_str = brt_dt.strftime("%b %d")
        ha_icon  = "🏠" if g["is_home"] else "✈️"
        ha_str   = "vs" if g["is_home"] else "@"
        comp     = g["comp"]

        if g["completed"]:
            r = g["result"]
            badge_cls  = {"W": "bw", "D": "bd", "L": "bl"}.get(r, "bx")
            card_cls   = {"W": "cw", "D": "cd", "L": "cl"}.get(r, "cx")
            badge_text = r or "?"
            score_html = f'<div class="score">{g["crz_goals"]} – {g["opp_goals"]}</div>'
            time_html  = f'<div class="ct">{date_str}</div>'
        else:
            badge_cls  = "bu"
            card_cls   = "cu"
            badge_text = "Next"
            score_html = ""
            t = brt_dt.strftime("%H:%M") if brt_dt.hour != 0 or brt_dt.minute != 0 else "TBD"
            time_html  = f'<div class="ct">{t}<br>BRT</div>'

        rnd_html = f' <span class="rnd">· {g["round"]}</span>' if g["round"] else ""

        return (f'<div class="card {card_cls}">'
                f'<div class="cl2"><span class="badge {badge_cls}">{badge_text}</span>'
                f'<div class="cdate">{date_str}</div></div>'
                f'<div class="cc">'
                f'<div class="mu">{ha_icon} Cruzeiro <span class="vs">{ha_str}</span> {g["opponent"]}</div>'
                f'<div class="meta"><span class="ctag" style="color:{comp["color"]}">'
                f'{comp["emoji"]} {comp["name"]}</span>{rnd_html}</div>'
                f'{score_html}</div>'
                f'<div class="cr">{time_html}</div></div>')

    up_html = ""
    if upcoming:
        cards  = "".join(game_card(g) for g in upcoming)
        up_html = f'<h2 class="sec">Upcoming</h2>{cards}'

    res_html = "".join(game_card(g) for g in reversed(past))
    if not res_html:
        res_html = '<div class="empty">No results yet this season</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>⚽️ Cruzeiro — 2026 Season</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:32px 16px 64px}}
.wrap{{max-width:760px;margin:0 auto}}
.hdr{{text-align:center;margin-bottom:32px}}
.hdr h1{{font-size:2rem;font-weight:700;color:#fff}}
.hdr .sub{{color:#718096;margin-top:4px;font-size:.9rem}}
.stats{{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-bottom:32px}}
.stat{{background:#1a1f2e;border:1px solid #2d3748;border-radius:12px;padding:14px 20px;text-align:center;min-width:88px}}
.sv{{font-size:1.75rem;font-weight:700;line-height:1}}
.sl{{font-size:.7rem;color:#718096;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}}
.sw .sv{{color:#48bb78}}.sd .sv{{color:#ecc94b}}.sll .sv{{color:#fc8181}}
.sec{{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#718096;margin:24px 0 10px}}
.card{{display:flex;align-items:center;gap:14px;background:#1a1f2e;border:1px solid #2d3748;
       border-left:4px solid transparent;border-radius:12px;padding:13px 16px;margin-bottom:8px;
       transition:border-color .15s}}
.card:hover{{border-color:#4a5568}}
.cw{{border-left-color:#48bb78}}.cd{{border-left-color:#ecc94b}}.cl{{border-left-color:#fc8181}}
.cu{{border-left-color:#63b3ed;background:#151e2e}}.cx{{border-left-color:#4a5568}}
.cl2{{display:flex;flex-direction:column;align-items:center;gap:5px;min-width:44px}}
.cdate{{font-size:.68rem;color:#718096;white-space:nowrap}}
.cc{{flex:1;min-width:0}}
.mu{{font-size:.95rem;font-weight:600;color:#e2e8f0}}
.vs{{color:#718096;font-weight:400}}
.meta{{font-size:.75rem;color:#718096;margin-top:3px}}
.ctag{{font-weight:600}}
.rnd{{color:#4a5568}}
.score{{font-size:.85rem;color:#a0aec0;margin-top:4px;font-variant-numeric:tabular-nums}}
.cr{{text-align:right;min-width:52px;flex-shrink:0}}
.ct{{font-size:.72rem;color:#718096;line-height:1.4}}
.badge{{display:inline-block;font-size:.62rem;font-weight:700;text-transform:uppercase;
        letter-spacing:.04em;padding:2px 7px;border-radius:5px}}
.bw{{background:#22543d;color:#68d391}}.bd{{background:#744210;color:#f6e05e}}
.bl{{background:#742a2a;color:#fc8181}}.bu{{background:#1a365d;color:#63b3ed}}
.bx{{background:#2d3748;color:#718096}}
.empty{{color:#4a5568;font-size:.9rem;padding:20px 0}}
.footer{{text-align:center;margin-top:40px;font-size:.72rem;color:#4a5568}}
@media(max-width:500px){{.cr{{display:none}}}}
</style>
</head>
<body>
<div class="wrap">
<div class="hdr"><h1>⚽️ Cruzeiro EC</h1><div class="sub">2026 Season</div></div>
<div class="stats">
  <div class="stat"><div class="sv">{wins}–{draws}–{losses}</div><div class="sl">W – D – L</div></div>
  <div class="stat sw"><div class="sv">{wins}</div><div class="sl">Wins</div></div>
  <div class="stat sd"><div class="sv">{draws}</div><div class="sl">Draws</div></div>
  <div class="stat sll"><div class="sv">{losses}</div><div class="sl">Losses</div></div>
  <div class="stat"><div class="sv">{gf}:{ga}</div><div class="sl">Goals</div></div>
  <div class="stat"><div class="sv">{len(upcoming)}</div><div class="sl">Upcoming</div></div>
</div>
{up_html}
<h2 class="sec">Results — most recent first</h2>
{res_html}
<div class="footer">Updated {generated} · source: ESPN</div>
</div>
</body>
</html>"""


def build_cruzeiro_html():
    print("  Fetching Cruzeiro results...")
    games = fetch_cruzeiro_games()
    html  = build_html(games)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    past     = sum(1 for g in games if g["completed"])
    upcoming = sum(1 for g in games if not g["completed"])
    print(f"  ✅ Dashboard → {OUTPUT_HTML}  ({past} results · {upcoming} upcoming)")


if __name__ == "__main__":
    build_cruzeiro_html()
