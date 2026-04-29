#!/usr/bin/env python3
"""
furia_dashboard.py
Generates furia_results.html — FURIA CS2's 2026 season results & schedule.
"""

import gzip
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

LIQUIPEDIA_API = "https://liquipedia.net/counterstrike/api.php"
FURIA_LP_PAGE  = "FURIA"
OUTPUT_HTML    = Path.home() / "Library/Application Support/CruzeiroCalendar/furia_results.html"
BRT            = timezone(timedelta(hours=-3))


def _lp_fetch(page: str) -> str:
    url = (f"{LIQUIPEDIA_API}?action=parse&page={urllib.request.quote(page)}"
           f"&prop=text&format=json")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Encoding": "gzip",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                body = gzip.decompress(raw)
            except Exception:
                body = raw
            return json.loads(body).get("parse", {}).get("text", {}).get("*", "")
    except Exception:
        return ""


def _extract_score(chunk: str, furia_is_left: bool):
    """
    Try to extract map score from match HTML chunk.
    Returns (furia_maps, opp_maps) or None.
    """
    # Liquipedia shows scores as "2 : 0" or in bracket cells
    m = re.search(r'(?<!\d)(\d)\s*:\s*(\d)(?!\d)', chunk)
    if m:
        l, r = int(m.group(1)), int(m.group(2))
        return (l, r) if furia_is_left else (r, l)
    return None


def _find_tournament_pages(html: str) -> list:
    """
    Find tournament page links from both Upcoming and Results sections on FURIA's page.
    Returns list of (path, is_upcoming) tuples.
    """
    pages, seen = [], set()
    for section_tag, is_upcoming in [
        ("Upcoming", True),
        ("Results",  False),
        ("Recent",   False),
        ("History",  False),
    ]:
        idx = html.find(section_tag)
        if idx < 0:
            continue
        section = html[idx: idx + 10000]
        for m in re.finditer(r'<a href="/counterstrike/([^"#]+)"', section):
            path = m.group(1)
            if any(skip in path for skip in ("Category", "Tier", "Tournaments", "http", "FURIA")):
                continue
            if path not in seen:
                seen.add(path)
                pages.append((path, is_upcoming))
    return pages


def _parse_bracket_results(html: str, tourney_name: str, seen_key: set) -> list:
    """
    Parse completed bracket match blocks — Liquipedia uses 'brkts-opponent-score-bold'
    for the winning score instead of data-timestamp on completed matches.
    Each match block contains two opponent entries with team name + score.
    """
    results = []
    # A completed match block looks like:
    # <div class="brkts-match">
    #   <div class="brkts-opponent-entry"> ... team1 ... score1 ... </div>
    #   <div class="brkts-opponent-entry"> ... team2 ... score2 ... </div>
    # </div>
    for block_m in re.finditer(r'<div[^>]+class="[^"]*brkts-match[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL):
        block = block_m.group(0)
        if "FURIA" not in block:
            continue

        # Extract both opponent entries
        entries = re.findall(r'brkts-opponent-entry.*?(?=brkts-opponent-entry|$)', block, re.DOTALL)
        if len(entries) < 2:
            continue

        def parse_entry(e):
            name_m  = re.search(r'title="([^"]+)"', e)
            score_m = re.search(r'brkts-opponent-score[^>]*>\s*<span[^>]*>(\d+)<', e)
            bold_m  = re.search(r'brkts-opponent-score-bold[^>]*>\s*<span[^>]*>(\d+)<', e)
            name  = name_m.group(1) if name_m else ""
            score = int((bold_m or score_m).group(1)) if (bold_m or score_m) else None
            won   = bold_m is not None
            return name, score, won

        n1, s1, w1 = parse_entry(entries[0])
        n2, s2, w2 = parse_entry(entries[1])

        if not n1 or not n2 or s1 is None or s2 is None:
            continue
        if "FURIA" not in (n1, n2) and "FURIA Esports" not in (n1, n2):
            continue

        furia_first = "FURIA" in n1 or "FURIA Esports" in n1
        furia_maps  = s1 if furia_first else s2
        opp_maps    = s2 if furia_first else s1
        furia_won   = w1 if furia_first else w2
        opponent    = n2 if furia_first else n1

        if not opponent or opponent.upper() in ("TBD", "BYE"):
            continue

        key = f"{opponent}-{furia_maps}-{opp_maps}-{tourney_name}"
        if key in seen_key:
            continue
        seen_key.add(key)

        result = "W" if furia_maps > opp_maps else ("L" if furia_maps < opp_maps else "D")
        results.append({
            "dt":         None,   # no timestamp on completed bracket matches
            "tournament": tourney_name,
            "opponent":   opponent,
            "bo":         None,
            "completed":  True,
            "upcoming":   False,
            "result":     result,
            "furia_maps": furia_maps,
            "opp_maps":   opp_maps,
        })
    return results


def fetch_furia_matches():
    now_ts = int(datetime.now(timezone.utc).timestamp())

    furia_html = _lp_fetch(FURIA_LP_PAGE)
    if not furia_html:
        print("  ⚠️  Could not fetch FURIA Liquipedia page", file=sys.stderr)
        return []

    tourney_pages = _find_tournament_pages(furia_html)
    print(f"  🎮 Scanning {len(tourney_pages)} tournament page(s)...")

    matches, seen_ts, seen_key = [], set(), set()

    for page, _ in tourney_pages:
        html = _lp_fetch(page)
        if not html:
            continue

        tourney_name = page.split("/")[-1].replace("_", " ")

        # ── Upcoming: data-timestamp entries ─────────────────────
        for m in re.finditer(r'data-timestamp="(\d+)"', html):
            ts = int(m.group(1))
            if ts in seen_ts:
                continue

            chunk = html[max(0, m.start() - 200): m.start() + 3000]
            if "FURIA" not in chunk:
                continue

            team_entries = re.findall(
                r'class="name"[^>]*>\s*<a[^>]*title="([^"]+)"[^>]*>([^<]+)</a>',
                chunk
            )
            full_names  = [t for t, _ in team_entries]
            short_names = [s for _, s in team_entries]

            opponent = None
            for full, short in zip(full_names, short_names):
                if full not in ("FURIA", "FURIA Esports") and short not in ("FURIA",):
                    opponent = full
                    break

            if not opponent or opponent.upper() in ("TBD", "BYE", ""):
                continue

            bo_m = re.search(r'Bo(\d)', chunk, re.IGNORECASE)
            bo   = int(bo_m.group(1)) if bo_m else None

            is_past = ts < now_ts
            furia_pos  = min((chunk.find(f) for f in ["FURIA Esports", "FURIA"] if chunk.find(f) >= 0), default=0)
            opp_pos    = chunk.find(opponent)
            furia_left = furia_pos < opp_pos if opp_pos >= 0 else True
            score  = _extract_score(chunk, furia_left) if is_past else None
            result = None
            if is_past and score:
                fm, om = score
                result = "W" if fm > om else ("L" if fm < om else "D")

            match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            matches.append({
                "dt":         match_dt,
                "tournament": tourney_name,
                "opponent":   opponent,
                "bo":         bo,
                "completed":  is_past,
                "upcoming":   not is_past,
                "result":     result,
                "furia_maps": score[0] if score else None,
                "opp_maps":   score[1] if score else None,
            })
            seen_ts.add(ts)
            brt_str = match_dt.astimezone(BRT).strftime("%b %d %H:%M")
            status  = f"{result} {score[0]}-{score[1]}" if score else ("upcoming" if not is_past else "played")
            print(f"    → {'✅' if is_past else '⏳'} FURIA vs {opponent:<20} [{tourney_name}]  [{brt_str} BRT]  {status}")

        # ── Past: bracket score blocks (no timestamp on completed matches) ──
        bracket_results = _parse_bracket_results(html, tourney_name, seen_key)
        for r in bracket_results:
            matches.append(r)
            print(f"    → ✅ FURIA vs {r['opponent']:<20} [{tourney_name}]  {r['result']} {r['furia_maps']}-{r['opp_maps']}")

    # Sort: completed without dt go to end of past section
    matches.sort(key=lambda x: (x["upcoming"], x["dt"] or datetime.min.replace(tzinfo=timezone.utc)))
    return matches


def _nav(active: str) -> str:
    items = [
        ("/cruzeiro_results.html",  "⚽️ Cruzeiro"),
        ("/fonseca.html",           "🎾 Fonseca"),
        ("/furia_results.html",     "🎮 FURIA"),
        ("/calendar_changelog.html","📋 Activity"),
        ("/changelog.html",         "🛠 Changelog"),
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


def build_html(matches) -> str:
    past     = [m for m in matches if m["completed"]]
    upcoming = [m for m in matches if m["upcoming"]]

    wins     = sum(1 for m in past if m["result"] == "W")
    losses   = sum(1 for m in past if m["result"] == "L")
    with_score = sum(1 for m in past if m["furia_maps"] is not None)
    generated  = datetime.now(BRT).strftime("%b %d, %Y · %H:%M BRT")

    def match_card(m):
        brt_dt   = m["dt"].astimezone(BRT) if m["dt"] else None
        date_str = brt_dt.strftime("%b %d") if brt_dt else "—"
        bo_html  = f' <span class="rnd">· Bo{m["bo"]}</span>' if m["bo"] else ""

        if m["completed"]:
            r = m["result"]
            badge_cls  = {"W": "bw", "L": "bl", "D": "bd"}.get(r, "bx")
            card_cls   = {"W": "cw", "L": "cl", "D": "cd"}.get(r, "cx")
            badge_text = r or "?"
            if m["furia_maps"] is not None:
                score_html = f'<div class="score">{m["furia_maps"]} – {m["opp_maps"]} maps</div>'
            else:
                score_html = '<div class="score noscore">Score unavailable</div>'
            time_html = f'<div class="ct">{date_str}</div>'
        else:
            badge_cls, card_cls, badge_text = "bu", "cu", "Next"
            score_html = ""
            t = brt_dt.strftime("%H:%M") if brt_dt else "TBD"
            time_html  = f'<div class="ct">{t}<br>BRT</div>'

        return (f'<div class="card {card_cls}">'
                f'<div class="cl2"><span class="badge {badge_cls}">{badge_text}</span>'
                f'<div class="cdate">{date_str}</div></div>'
                f'<div class="cc">'
                f'<div class="mu">🎮 FURIA <span class="vs">vs</span> {m["opponent"]}</div>'
                f'<div class="meta">{m["tournament"]}{bo_html}</div>'
                f'{score_html}</div>'
                f'<div class="cr">{time_html}</div></div>')

    up_html = ""
    if upcoming:
        cards  = "".join(match_card(m) for m in upcoming)
        up_html = f'<h2 class="sec">Upcoming</h2>{cards}'

    res_html = "".join(match_card(m) for m in reversed(past))
    if not res_html:
        res_html = '<div class="empty">No results yet this season</div>'

    score_note = (f'<span style="color:#4a5568"> · scores parsed for {with_score}/{len(past)} matches</span>'
                  if past else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>🎮 FURIA CS2 — 2026</title>
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
.sw .sv{{color:#48bb78}}.sll .sv{{color:#fc8181}}
.sec{{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#718096;margin:24px 0 10px}}
.card{{display:flex;align-items:center;gap:14px;background:#1a1f2e;border:1px solid #2d3748;
       border-left:4px solid transparent;border-radius:12px;padding:13px 16px;margin-bottom:8px;
       transition:border-color .15s}}
.card:hover{{border-color:#4a5568}}
.cw{{border-left-color:#48bb78}}.cd{{border-left-color:#ecc94b}}.cl{{border-left-color:#fc8181}}
.cu{{border-left-color:#b48efa;background:#13132a}}.cx{{border-left-color:#4a5568}}
.cl2{{display:flex;flex-direction:column;align-items:center;gap:5px;min-width:44px}}
.cdate{{font-size:.68rem;color:#718096;white-space:nowrap}}
.cc{{flex:1;min-width:0}}
.mu{{font-size:.95rem;font-weight:600;color:#e2e8f0}}
.vs{{color:#718096;font-weight:400}}
.meta{{font-size:.75rem;color:#718096;margin-top:3px}}
.rnd{{color:#4a5568}}
.score{{font-size:.82rem;color:#a0aec0;margin-top:4px;font-variant-numeric:tabular-nums}}
.noscore{{color:#3a3f52;font-style:italic}}
.cr{{text-align:right;min-width:52px;flex-shrink:0}}
.ct{{font-size:.72rem;color:#718096;line-height:1.4}}
.badge{{display:inline-block;font-size:.62rem;font-weight:700;text-transform:uppercase;
        letter-spacing:.04em;padding:2px 7px;border-radius:5px}}
.bw{{background:#22543d;color:#68d391}}.bd{{background:#744210;color:#f6e05e}}
.bl{{background:#742a2a;color:#fc8181}}.bu{{background:#2d1b5e;color:#b48efa}}
.bx{{background:#2d3748;color:#718096}}
.empty{{color:#4a5568;font-size:.9rem;padding:20px 0}}
.footer{{text-align:center;margin-top:40px;font-size:.72rem;color:#4a5568}}
@media(max-width:500px){{.cr{{display:none}}}}
</style>
</head>
<body>
{_nav("/furia_results.html")}
<div class="wrap">
<div class="hdr"><h1>🎮 FURIA CS2</h1><div class="sub">2026 Season</div></div>
<div class="stats">
  <div class="stat"><div class="sv">{wins}–{losses}</div><div class="sl">W – L</div></div>
  <div class="stat sw"><div class="sv">{wins}</div><div class="sl">Wins</div></div>
  <div class="stat sll"><div class="sv">{losses}</div><div class="sl">Losses</div></div>
  <div class="stat"><div class="sv">{len(past)}</div><div class="sl">Played</div></div>
  <div class="stat"><div class="sv">{len(upcoming)}</div><div class="sl">Upcoming</div></div>
</div>
{up_html}
<h2 class="sec">Results — most recent first</h2>
{res_html}
<div class="footer">Updated {generated} · source: Liquipedia{score_note}</div>
</div>
</body>
</html>"""


def build_furia_html():
    print("  Fetching FURIA CS2 results...")
    matches = fetch_furia_matches()
    html    = build_html(matches)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    past     = sum(1 for m in matches if m["completed"])
    upcoming = sum(1 for m in matches if m["upcoming"])
    print(f"  ✅ Dashboard → {OUTPUT_HTML}  ({past} results · {upcoming} upcoming)")


if __name__ == "__main__":
    build_furia_html()
