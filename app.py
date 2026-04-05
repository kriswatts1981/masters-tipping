#!/usr/bin/env python3
"""
Masters Tipping Competition — Live Dashboard
Pulls ESPN leaderboard, calculates punter standings with cut rule.
Run: python3 app.py
Open: http://localhost:8060
"""

import json
import os
import time
import requests
from flask import Flask, render_template_string, request, send_from_directory

app = Flask(__name__)


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(DIR, "static"), filename)

# ─── Config ─────────────────────────────────────────────────────────────────
DIR = os.path.dirname(os.path.abspath(__file__))
PICKS_FILE = os.path.join(DIR, "picks.json")
CONFIG_FILE = os.path.join(DIR, "config.json")
ODDS_FILE = os.path.join(DIR, "odds.json")

# Default config — update tournament_id when Masters field is confirmed
DEFAULT_CONFIG = {
    "tournament_id": "401811941",  # UPDATE to Masters 2026 tournament ID
    "tournament_name": "The Masters 2026",
    "buy_in": 50,
    "payout_places": 20,
    "espn_api": "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard",
}

# ─── Cache ──────────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0}
_weather_cache = {"data": None, "ts": 0}
CACHE_TTL = 60  # refresh every 60 seconds
WEATHER_TTL = 600  # refresh weather every 10 minutes

# Augusta National: 33.47°N, 81.97°W, timezone America/New_York
AUGUSTA_LAT = 33.47
AUGUSTA_LON = -81.97
AUGUSTA_TZ = "America/New_York"


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(json.load(f))
            return cfg
    return DEFAULT_CONFIG.copy()


def load_picks():
    if os.path.exists(PICKS_FILE):
        with open(PICKS_FILE) as f:
            return json.load(f)
    return []


def load_odds():
    if os.path.exists(ODDS_FILE):
        with open(ODDS_FILE) as f:
            return json.load(f)
    return []


def fetch_weather():
    """Fetch current weather for Augusta, GA from Open-Meteo (free, no API key)."""
    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["ts"] < WEATHER_TTL:
        return _weather_cache["data"]

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={AUGUSTA_LAT}&longitude={AUGUSTA_LON}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation_probability,"
            f"wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code"
            f"&temperature_unit=celsius&wind_speed_unit=mph&timezone={AUGUSTA_TZ}"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        c = data.get("current", {})

        # Wind direction to compass
        deg = c.get("wind_direction_10m", 0)
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        wind_dir = dirs[int((deg + 11.25) / 22.5) % 16]

        # Weather code to description
        wmo = {0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
               45: "Fog", 48: "Rime Fog", 51: "Light Drizzle", 53: "Drizzle",
               55: "Heavy Drizzle", 61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
               71: "Light Snow", 73: "Snow", 80: "Light Showers", 81: "Showers",
               82: "Heavy Showers", 95: "Thunderstorm", 96: "Thunderstorm + Hail"}
        code = c.get("weather_code", 0)
        condition = wmo.get(code, "Unknown")

        # Weather emoji
        emojis = {0: "sun", 1: "sun", 2: "cloud-sun", 3: "cloud",
                  45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
                  61: "rain", 63: "rain", 65: "rain", 80: "rain", 81: "rain",
                  95: "storm", 96: "storm"}

        weather = {
            "temp_c": round(c.get("temperature_2m", 0)),
            "temp_f": round(c.get("temperature_2m", 0) * 9 / 5 + 32),
            "humidity": c.get("relative_humidity_2m", 0),
            "precipitation": c.get("precipitation_probability", 0),
            "wind_speed": round(c.get("wind_speed_10m", 0)),
            "wind_dir": wind_dir,
            "gusts": round(c.get("wind_gusts_10m", 0)),
            "condition": condition,
            "code": code,
            "local_time": c.get("time", ""),
        }
        _weather_cache["data"] = weather
        _weather_cache["ts"] = now
        return weather
    except Exception as e:
        print(f"Weather API error: {e}")
        return _weather_cache["data"] or {}


def fetch_leaderboard():
    """Fetch live leaderboard from ESPN API. Returns dict of player_name -> score_data."""
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    cfg = load_config()
    params = f"tournamentId={cfg['tournament_id']}"
    if cfg.get("espn_date"):
        params += f"&dates={cfg['espn_date']}"
    url = f"{cfg['espn_api']}?{params}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"ESPN API error: {e}")
        return _cache["data"] or {}

    events = data.get("events", [])
    if not events:
        return _cache["data"] or {}

    event = events[0]
    competition = event.get("competitions", [{}])[0]
    competitors = competition.get("competitors", [])

    # Tournament status
    status = competition.get("status", {})
    status_type = status.get("type", {}).get("name", "STATUS_SCHEDULED")
    current_round = status.get("period", 0)

    # Determine cut line (after round 2)
    cut_line = None
    if current_round >= 3 or status_type in ("STATUS_FINAL", "STATUS_PLAY_COMPLETE"):
        # Find the cut line from players who missed the cut
        scores_of_cut_players = []
        for c in competitors:
            player_status = c.get("status", {}).get("type", {}).get("name", "")
            if player_status == "cut":
                # Their score after R2 is the cut line
                linescores = c.get("linescores", [])
                if len(linescores) >= 2:
                    r1 = linescores[0].get("value", 0)
                    r2 = linescores[1].get("value", 0)
                    scores_of_cut_players.append(r1 + r2)
        if scores_of_cut_players:
            cut_line = min(scores_of_cut_players)  # best score among cut players = the cut line

    players = {}
    for c in competitors:
        athlete = c.get("athlete", {})
        name = athlete.get("displayName", "Unknown")
        flag_data = athlete.get("flag", {})
        country = flag_data.get("alt", "")
        flag_url = flag_data.get("href", "")
        score_str = c.get("score", "E")
        position = c.get("status", {}).get("position", {}).get("displayName", "")
        player_status = c.get("status", {}).get("type", {}).get("name", "")
        thru = c.get("status", {}).get("thru", 0)
        today_score = c.get("status", {}).get("todayScore", "")

        # Parse total score relative to par
        if score_str == "E":
            total_score = 0
        else:
            try:
                total_score = int(score_str)
            except (ValueError, TypeError):
                total_score = 0

        # Round scores
        linescores = c.get("linescores", [])
        rounds = []
        for ls in linescores:
            val = ls.get("value", 0)
            rounds.append(int(val) if val else 0)

        # Calculate score after R2 (for cut rule)
        r2_total = sum(rounds[:2]) if len(rounds) >= 2 else total_score

        # Apply cut rule:
        # If player made the cut but blew out in R3/R4, cap at cut line
        # If player missed cut, use their R2 total
        effective_score = total_score
        if player_status == "cut":
            # Missed the cut — use their score after R2
            effective_score = r2_total
        elif cut_line is not None and current_round >= 3 and player_status != "cut":
            # Made the cut — if their total is worse than cut line, cap at cut line
            if total_score > cut_line:
                effective_score = cut_line

        players[name] = {
            "name": name,
            "score": total_score,
            "effective_score": effective_score,
            "score_display": score_str,
            "position": position,
            "status": player_status,
            "thru": thru,
            "today": today_score,
            "rounds": rounds,
            "r2_total": r2_total,
            "cut": player_status == "cut",
            "country": country,
            "flag_url": flag_url,
        }

    # Calculate positions from scores if ESPN doesn't provide them
    sorted_players = sorted(players.values(), key=lambda x: x["score"])
    calc_pos = 1
    for i, p in enumerate(sorted_players):
        if i > 0 and p["score"] == sorted_players[i - 1]["score"]:
            p["calc_position"] = sorted_players[i - 1]["calc_position"]
        else:
            p["calc_position"] = calc_pos
        calc_pos = i + 2
        # Use ESPN position if available, otherwise calculated
        if not p["position"]:
            pos = p["calc_position"]
            # Check for ties
            tied = sum(1 for x in sorted_players if x["score"] == p["score"])
            p["position"] = f"T{pos}" if tied > 1 else str(pos)

    result = {
        "players": players,
        "cut_line": cut_line,
        "current_round": current_round,
        "status": status_type,
        "tournament_name": event.get("name", cfg["tournament_name"]),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
    }
    _cache["data"] = result
    _cache["ts"] = now
    return result


def _normalize(s):
    """Strip accents and special chars for fuzzy matching."""
    import unicodedata
    # Replace common non-decomposable chars
    s = s.replace('ø', 'o').replace('Ø', 'O').replace('ð', 'd').replace('æ', 'ae').replace('Æ', 'AE')
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn').lower()


def match_player(pick_name, players):
    """Fuzzy match a pick name to ESPN player names."""
    pick_lower = pick_name.strip().lower()
    pick_norm = _normalize(pick_name)
    # Exact match first
    for name in players:
        if name.lower() == pick_lower or _normalize(name) == pick_norm:
            return players[name]
    # Partial match
    for name in players:
        if pick_lower in name.lower() or name.lower() in pick_lower:
            return players[name]
    # Normalized partial match (accent-insensitive)
    for name in players:
        name_norm = _normalize(name)
        if pick_norm in name_norm or name_norm in pick_norm:
            return players[name]
    # Last name match
    pick_last = pick_norm.split()[-1] if pick_norm.split() else pick_norm
    for name in players:
        name_last = _normalize(name).split()[-1]
        if name_last == pick_last:
            return players[name]
    return None


def calculate_standings():
    """Calculate punter standings from picks + leaderboard."""
    lb = fetch_leaderboard()
    if not lb:
        return {"punters": [], "leaderboard": {}, "status": "No data"}

    picks = load_picks()
    cfg = load_config()
    players = lb["players"]

    punter_results = []
    for entry in picks:
        punter_name = entry["name"]
        player_picks = entry.get("picks", [])
        total = 0
        player_details = []
        for pick in player_picks:
            matched = match_player(pick, players)
            if matched:
                total += matched["effective_score"]
                player_details.append({
                    "pick": pick,
                    "name": matched["name"],
                    "score": matched["effective_score"],
                    "actual_score": matched["score"],
                    "display": matched["score_display"],
                    "position": matched["position"],
                    "cut": matched["cut"],
                    "capped": matched["effective_score"] != matched["score"],
                    "thru": matched["thru"],
                    "today": matched["today"],
                    "rounds": matched["rounds"],
                })
            else:
                player_details.append({
                    "pick": pick,
                    "name": pick,
                    "score": 0,
                    "actual_score": 0,
                    "display": "?",
                    "position": "N/A",
                    "cut": False,
                    "capped": False,
                    "thru": 0,
                    "today": "",
                    "rounds": [],
                })

        punter_results.append({
            "name": punter_name,
            "total": total,
            "players": player_details,
        })

    # Sort by total (lowest wins)
    punter_results.sort(key=lambda x: x["total"])

    # Assign positions (handle ties)
    pos = 1
    for i, p in enumerate(punter_results):
        if i > 0 and p["total"] == punter_results[i - 1]["total"]:
            p["position"] = punter_results[i - 1]["position"]
        else:
            p["position"] = pos
        pos = i + 2

    # Prize pool
    total_entries = len(punter_results)
    prize_pool = total_entries * cfg["buy_in"]

    # Payout percentages matching the actual competition structure
    # Position: percentage of prize pool
    payout_pcts = {
        1: 35.0, 2: 17.5, 3: 10.0,
        4: 5.0, 5: 5.0, 6: 5.0, 7: 5.0, 8: 5.0,
        9: 2.5, 10: 2.5,
        11: 1.0, 12: 1.0, 13: 1.0, 14: 1.0, 15: 1.0,
        16: 0.5, 17: 0.5, 18: 0.5, 19: 0.5, 20: 0.5,
    }
    payouts = {}
    for pos_num, pct in payout_pcts.items():
        payouts[pos_num] = round(prize_pool * pct / 100, 0)

    # Mark who's in the money
    for p in punter_results:
        if p["position"] <= cfg["payout_places"]:
            p["payout"] = payouts.get(p["position"], 0)
        else:
            p["payout"] = 0

    # Build sorted tournament leaderboard for display
    tournament_lb = sorted(players.values(), key=lambda x: x["score"])

    # Track which players are picked and by how many punters
    picked_by = {}
    for entry in picks:
        for pick in entry.get("picks", []):
            matched = match_player(pick, players)
            if matched:
                picked_by.setdefault(matched["name"], []).append(entry["name"])

    # Pre-sorted popular players: [(name, count, pct), ...] by count desc
    popular_players = []
    for name, punters_list in picked_by.items():
        cnt = len(punters_list)
        pct = round((cnt / total_entries) * 100, 1) if total_entries else 0
        popular_players.append((name, cnt, pct))
    popular_players.sort(key=lambda x: x[1], reverse=True)

    # Calculate movers: compare R2-only standings to current standings
    # Build R2-only totals for each punter
    r2_results = []
    for p in punter_results:
        r2_total = 0
        for pl in p["players"]:
            # Sum only first 2 round scores for each player
            rounds = pl.get("rounds", [])
            if len(rounds) >= 2:
                r2_total += rounds[0] + rounds[1]
            else:
                r2_total += pl["score"]
        r2_results.append({"name": p["name"], "r2_total": r2_total})

    # Sort R2 results and assign R2 positions
    r2_results.sort(key=lambda x: x["r2_total"])
    r2_positions = {}
    r2_pos = 1
    for i, r in enumerate(r2_results):
        if i > 0 and r["r2_total"] == r2_results[i - 1]["r2_total"]:
            r2_positions[r["name"]] = r2_positions[r2_results[i - 1]["name"]]
        else:
            r2_positions[r["name"]] = r2_pos
        r2_pos = i + 2

    # Assign mover field (positive = moved up, negative = moved down)
    for p in punter_results:
        r2_rank = r2_positions.get(p["name"], p["position"])
        p["mover"] = r2_rank - p["position"]

    # Load odds and enrich with pick counts + tournament score
    # Build a normalized lookup for picked_by so accent-insensitive matching works
    picked_by_norm = {}
    for pname, punters_list in picked_by.items():
        picked_by_norm[_normalize(pname)] = punters_list

    odds = load_odds()
    for o in odds:
        # Try exact match first, then normalized
        o["pick_count"] = len(picked_by.get(o["player"], []))
        if o["pick_count"] == 0:
            norm_key = _normalize(o["player"])
            o["pick_count"] = len(picked_by_norm.get(norm_key, []))
        matched = match_player(o["player"], players)
        if matched:
            o["tournament_score"] = matched["score"]
            o["position"] = matched["position"]
            o["cut"] = matched["cut"]
        else:
            o["tournament_score"] = None
            o["position"] = ""
            o["cut"] = False

    return {
        "punters": punter_results,
        "leaderboard": lb,
        "tournament_lb": tournament_lb,
        "picked_by": picked_by,
        "picked_by_norm": {k.lower(): v for k, v in picked_by.items()},
        "popular_players": popular_players,
        "odds": odds,
        "weather": fetch_weather(),
        "prize_pool": prize_pool,
        "total_entries": total_entries,
        "payouts": payouts,
        "config": cfg,
    }


# ─── Template ───────────────────────────────────────────────────────────────
TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Masters Tipping 2026</title>
<meta http-equiv="refresh" content="120">
<link rel="icon" href="/static/favicon.png" type="image/png">
<link rel="apple-touch-icon" href="/static/favicon.png">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#006747;color:#fff;font-family:'Inter',sans-serif;min-height:100vh;}

/* Header - Augusta scoreboard inspired */
.hero{background:linear-gradient(170deg,#003a2b 0%,#005c3f 40%,#004d35 100%);padding:0;text-align:center;position:relative;overflow:hidden;}
.hero-bg{position:absolute;top:0;left:0;right:0;bottom:0;opacity:.12;}
/* CSS-only scoreboard grid pattern */
.hero-bg::before{content:'';position:absolute;top:-20px;right:-40px;width:70%;height:120%;
  background:
    repeating-linear-gradient(0deg,transparent,transparent 28px,rgba(255,255,255,.08) 28px,rgba(255,255,255,.08) 29px),
    repeating-linear-gradient(90deg,transparent,transparent 36px,rgba(255,255,255,.08) 36px,rgba(255,255,255,.08) 37px);
  transform:perspective(800px) rotateY(-12deg) rotateX(2deg);transform-origin:right center;opacity:.6;}
.hero-bg::after{content:'LEADERS';position:absolute;top:18px;right:80px;font-family:'Playfair Display',serif;font-size:32px;font-weight:900;letter-spacing:4px;color:rgba(255,255,255,.06);transform:perspective(800px) rotateY(-12deg);transform-origin:right center;}
.hero-content{position:relative;padding:18px 32px 16px;}
.hero-logo{display:flex;align-items:center;justify-content:center;gap:14px;margin-bottom:0;}
.hero-flag-icon{width:36px;height:42px;position:relative;flex-shrink:0;}
.hero-flag-icon .pole{position:absolute;left:50%;top:0;width:2px;height:100%;background:linear-gradient(180deg,#b8860b,#8b6914);border-radius:1px;}
.hero-flag-icon .flag{position:absolute;left:calc(50% + 1px);top:0;width:18px;height:13px;background:#ffd700;border-radius:0 2px 2px 0;box-shadow:0 1px 4px rgba(0,0,0,.3);}
.hero-flag-icon .flag::after{content:'';position:absolute;top:3px;left:4px;width:3px;height:3px;background:#006747;border-radius:50%;box-shadow:5px 0 0 #006747;}
.hero h1{font-family:'Playfair Display',serif;font-size:36px;color:#ffd700;letter-spacing:1.5px;font-weight:900;margin-bottom:0;text-shadow:0 2px 8px rgba(0,0,0,.3);}
.hero .sub{font-size:14px;color:rgba(255,255,255,.65);font-family:'Playfair Display',serif;font-style:italic;}
.hero .status-live{display:inline-block;background:#ef4444;color:#fff;font-family:'Inter',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;letter-spacing:1px;font-style:normal;animation:pulse 2s infinite;margin-left:6px;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.5;}}

/* Augusta scoreboard strip */
.scoreboard-strip{background:rgba(0,0,0,.35);border-top:1px solid rgba(255,215,0,.15);border-bottom:1px solid rgba(255,215,0,.15);padding:12px 20px;display:flex;justify-content:center;gap:3px;overflow-x:auto;backdrop-filter:blur(4px);}
.sb-cell{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:3px;min-width:56px;padding:6px 4px;text-align:center;}
.sb-cell .sb-name{font-size:9px;font-weight:600;letter-spacing:.5px;color:rgba(255,255,255,.6);text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:56px;}
.sb-cell .sb-score{font-size:16px;font-weight:800;font-family:'Courier New',monospace;margin-top:2px;}
.sb-cell.sb-leader{background:rgba(255,215,0,.1);border-color:rgba(255,215,0,.25);}
.sb-cell.sb-leader .sb-name{color:#ffd700;}

/* Stats bar */
.stats{display:flex;justify-content:center;gap:40px;padding:18px 20px;background:rgba(0,0,0,.25);flex-wrap:wrap;border-bottom:2px solid rgba(255,215,0,.3);}
.stat{text-align:center;}
.stat .val{font-size:24px;color:#ffd700;font-weight:700;font-family:'Playfair Display',serif;}
.stat .lbl{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:rgba(255,255,255,.5);margin-top:2px;}

/* Layout */
.container{max-width:1400px;margin:0 auto;padding:20px;}
.grid-2{display:grid;grid-template-columns:1fr 340px;gap:20px;}
@media(max-width:1200px){.grid-2{grid-template-columns:1fr 300px;}}
@media(max-width:1024px){.grid-2{grid-template-columns:1fr;}}
@media(max-width:768px){
  .hero-content{padding:20px 12px 16px;}
  .hero h1{font-size:24px;}
  .hero-bg::before{display:none;}
  .hero-bg::after{display:none;}
  .scoreboard-strip{padding:6px 8px;gap:2px;}
  .sb-cell{min-width:44px;padding:4px 2px;}
  .sb-cell .sb-name{font-size:7px;max-width:44px;}
  .sb-cell .sb-score{font-size:12px;}
  .stats{gap:8px 16px;padding:10px 8px;}
  .stat .val{font-size:16px;}
  .stat .lbl{font-size:7px;letter-spacing:.5px;}
  .container{padding:8px;}
  .card{margin-bottom:12px;border-radius:6px;}
  .card-title{padding:10px 12px;font-size:12px;}
  .card-title .badge{font-size:9px;padding:2px 7px;}
  .search{padding:6px 10px;}
  .search input{padding:6px 10px;font-size:12px;}
  th{padding:5px 6px;font-size:8px;}
  td{padding:4px 6px;font-size:10px;}
  .punter{font-size:10px;}
  .pick-cell{font-size:9px;}
  .pick-cell .pick-name{font-size:9px;}
  .pick-cell .pick-score{font-size:8px;}
  .pos-num{font-size:10px;}
  .sc{font-size:11px!important;}
  .scroll-table{max-height:500px;}
  .tlb-name{font-size:11px;}
  .tlb-picked{font-size:8px;padding:1px 4px;}
  .tlb-rounds{font-size:9px;}
  .tlb-thru{font-size:9px;}
  .footer{font-size:9px;padding:12px 8px;}
  /* Hide less important columns on mobile */
  .hide-mobile{display:none!important;}
  /* Weather bar stacks */
  .weather-bar{flex-direction:column;gap:4px!important;padding:6px 12px!important;}
  .weather-bar .weather-left{gap:8px!important;}
  .weather-bar .weather-right{gap:10px!important;font-size:11px!important;flex-wrap:wrap;}
}

/* Cards */
.card{background:rgba(0,0,0,.2);border-radius:8px;overflow:hidden;margin-bottom:20px;border:1px solid rgba(255,255,255,.08);backdrop-filter:blur(10px);}
.card-title{padding:14px 18px;font-size:14px;font-weight:700;color:#ffd700;border-bottom:1px solid rgba(255,255,255,.08);display:flex;justify-content:space-between;align-items:center;font-family:'Playfair Display',serif;letter-spacing:.5px;}
.card-title .badge{font-size:10px;background:rgba(255,215,0,.12);color:#ffd700;padding:3px 10px;border-radius:12px;font-family:'Inter',sans-serif;font-weight:600;}

/* Tables */
table{width:100%;border-collapse:collapse;}
th{text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:rgba(255,255,255,.4);padding:10px 10px;border-bottom:1px solid rgba(255,255,255,.08);font-weight:600;}
th.r,td.r{text-align:right;}
th.c,td.c{text-align:center;}
td{padding:7px 10px;border-bottom:1px solid rgba(255,255,255,.04);font-size:12px;}
tr:hover{background:rgba(255,255,255,.03);}

/* Scores */
.sc{font-family:'Courier New',monospace;font-weight:700;}
.under{color:#4ade80;}
.even{color:rgba(255,255,255,.8);}
.over{color:#f87171;}
.cut-txt{color:rgba(255,255,255,.3);text-decoration:line-through;}
.cap-txt{color:#fb923c;}

/* Tipping table */
.pos-num{font-weight:700;color:#ffd700;min-width:28px;display:inline-block;}
.punter{font-weight:600;font-size:13px;}
.payout-row{background:rgba(255,215,0,.04);}
.money{color:#ffd700;font-weight:700;}
.pick-cell{font-size:11px;line-height:1.4;}
.pick-name{font-weight:500;}
.pick-score{font-size:10px;opacity:.7;}

/* Tournament leaderboard */
.tlb-pos{font-weight:600;color:rgba(255,255,255,.5);min-width:24px;display:inline-block;font-size:11px;}
.tlb-name{font-weight:500;white-space:nowrap;}
.tlb-picked{font-size:9px;color:#ffd700;background:rgba(255,215,0,.1);padding:1px 5px;border-radius:3px;margin-left:4px;white-space:nowrap;vertical-align:middle;}
.tlb-rounds{font-size:10px;color:rgba(255,255,255,.4);}
.tlb-cut-row td{opacity:.4;}
.tlb-thru{font-size:10px;color:rgba(255,255,255,.5);}

/* Search */
.search{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.06);}
.search input{width:100%;padding:7px 12px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:#fff;font-size:12px;font-family:'Inter',sans-serif;}
.search input::placeholder{color:rgba(255,255,255,.25);}
.search input:focus{outline:none;border-color:rgba(255,215,0,.3);}

/* Tabs */
.tabs{display:flex;border-bottom:1px solid rgba(255,255,255,.08);}
.tab{padding:10px 18px;font-size:12px;font-weight:600;color:rgba(255,255,255,.4);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;}
.tab:hover{color:rgba(255,255,255,.7);}
.tab.active{color:#ffd700;border-bottom-color:#ffd700;}

.footer{text-align:center;padding:16px;font-size:10px;color:rgba(255,255,255,.2);letter-spacing:.5px;}
.empty{text-align:center;padding:40px;color:rgba(255,255,255,.3);}
.empty p{margin-bottom:6px;}

/* Scrollable */
.scroll-table{max-height:600px;overflow-y:auto;}
.scroll-table::-webkit-scrollbar{width:4px;}
.scroll-table::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:2px;}
</style>
</head>
<body>

<div class="hero">
  <div class="hero-bg"></div>
  <div class="hero-content">
    <div class="hero-logo">
      <div class="hero-flag-icon"><div class="pole"></div><div class="flag"></div></div>
      <h1>Masters Tipping 2026</h1>
    </div>
    <div class="sub">
      {% if data.leaderboard.status == 'STATUS_IN_PROGRESS' %}
        Round {{ data.leaderboard.current_round }} <span class="status-live">LIVE</span>
      {% elif data.leaderboard.status == 'STATUS_FINAL' %}Final Results
      {% elif data.leaderboard.status == 'STATUS_PLAY_COMPLETE' %}Play Complete
      {% elif data.leaderboard.status == 'STATUS_SUSPENDED' %}Play Suspended
      {% else %}Tournament begins April 10{% endif %}
      {% if data.leaderboard.cut_line is not none %} &middot; Cut Line: {{ "+" if data.leaderboard.cut_line > 0 }}{{ data.leaderboard.cut_line if data.leaderboard.cut_line != 0 else "E" }}{% endif %}
    </div>
  </div>
  <!-- Augusta-style scoreboard strip showing top 10 tournament leaders -->
  <div class="scoreboard-strip">
    {% for pl in data.tournament_lb[:12] %}
    <div class="sb-cell {% if loop.index <= 3 %}sb-leader{% endif %}">
      <div class="sb-name">{{ pl.name.split(' ')[-1] }}</div>
      <div class="sb-score {% if pl.score < 0 %}under{% elif pl.score == 0 %}even{% else %}over{% endif %}">{% if pl.score > 0 %}+{% endif %}{{ pl.score if pl.score != 0 else 'E' }}</div>
    </div>
    {% endfor %}
  </div>
</div>

<!-- Weather & Local Time Bar -->
{% if data.weather %}
<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 24px;background:rgba(0,0,0,.15);border-bottom:1px solid rgba(255,255,255,.06);flex-wrap:wrap;gap:8px;">
  <div style="display:flex;align-items:center;gap:16px;">
    <div style="font-size:10px;color:rgba(255,255,255,.35);text-transform:uppercase;letter-spacing:1px;">Augusta National</div>
    {% if data.weather.local_time %}
    <div style="font-size:12px;color:rgba(255,255,255,.6);font-family:'Courier New',monospace;">
      {% set parts = data.weather.local_time.split('T') %}
      {{ parts[1] if parts|length > 1 else '' }} ET
      <span style="color:rgba(255,255,255,.3);margin-left:4px;">{{ parts[0] if parts else '' }}</span>
    </div>
    {% endif %}
  </div>
  <div style="display:flex;align-items:center;gap:20px;font-size:12px;">
    <div style="display:flex;align-items:center;gap:6px;">
      <span style="font-size:20px;font-weight:700;color:#ffd700;">{{ data.weather.temp_c }}&deg;</span>
      <span style="font-size:10px;color:rgba(255,255,255,.4);">{{ data.weather.temp_f }}&deg;F</span>
    </div>
    <div style="color:rgba(255,255,255,.5);display:flex;align-items:center;gap:5px;">
      {% set wc = data.weather.code|default(0) %}
      {% if wc <= 1 %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffd700" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      {% elif wc == 2 %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffd700" stroke-width="2"><path d="M12 2v2M4.93 4.93l1.41 1.41M20 12h2M17.66 17.66l1.41 1.41M2 12h2M6.34 17.66l-1.41 1.41M17.07 4.93l1.41-1.41"/><circle cx="12" cy="12" r="4"/><path d="M16 17a5 5 0 0 0-8 0" stroke="rgba(255,255,255,.5)"/></svg>
      {% elif wc == 3 %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,.6)" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
      {% elif wc in (45, 48) %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,.4)" stroke-width="2"><line x1="3" y1="8" x2="21" y2="8"/><line x1="5" y1="12" x2="19" y2="12"/><line x1="7" y1="16" x2="17" y2="16"/></svg>
      {% elif wc in (51, 53, 55, 61, 63, 65, 80, 81, 82) %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" stroke="rgba(255,255,255,.5)"/><line x1="8" y1="19" x2="8" y2="22"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="16" y1="19" x2="16" y2="22"/></svg>
      {% elif wc in (95, 96) %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" stroke="rgba(255,255,255,.5)"/><path d="M13 16l-2 4h4l-2 4"/></svg>
      {% elif wc in (71, 73) %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#e0e7ff" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" stroke="rgba(255,255,255,.5)"/><circle cx="9" cy="20" r="1" fill="#e0e7ff"/><circle cx="13" cy="22" r="1" fill="#e0e7ff"/><circle cx="17" cy="20" r="1" fill="#e0e7ff"/></svg>
      {% else %}
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,.5)" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
      {% endif %}
      {{ data.weather.condition }}
    </div>
    <div style="color:rgba(255,255,255,.4);">
      <span style="color:rgba(255,255,255,.3);">Precip:</span> <span style="{% if data.weather.precipitation > 30 %}color:#60a5fa{% elif data.weather.precipitation > 60 %}color:#3b82f6{% endif %}">{{ data.weather.precipitation }}%</span>
    </div>
    <div style="color:rgba(255,255,255,.4);">
      <span style="color:rgba(255,255,255,.3);">Wind:</span> {{ data.weather.wind_dir }} {{ data.weather.wind_speed }} mph
    </div>
    <div style="color:rgba(255,255,255,.4);">
      <span style="color:rgba(255,255,255,.3);">Gusts:</span> {{ data.weather.gusts }} mph
    </div>
  </div>
</div>
{% endif %}

<div class="stats">
  <div class="stat"><div class="val">{{ data.total_entries }}</div><div class="lbl">Entries</div></div>
  <div class="stat"><div class="val">${{ "{:,.0f}".format(data.prize_pool) }}</div><div class="lbl">Prize Pool</div></div>
  <div class="stat"><div class="val">${{ "{:,.0f}".format(data.payouts.get(1, 0)) }}</div><div class="lbl">1st Place</div></div>
  <div class="stat"><div class="val">${{ "{:,.0f}".format(data.payouts.get(2, 0)) }}</div><div class="lbl">2nd Place</div></div>
  <div class="stat"><div class="val">${{ "{:,.0f}".format(data.payouts.get(3, 0)) }}</div><div class="lbl">3rd Place</div></div>
  <div class="stat"><div class="val">{{ data.config.payout_places }}</div><div class="lbl">Paid Places</div></div>
</div>

<div class="container">

<!-- Most Popular Players -->
{% if data.popular_players %}
<div class="card" style="margin-bottom:20px;">
  <div class="card-title">Most Selected Players <span class="badge">who's backing who</span></div>
  <div style="display:flex;gap:8px;padding:14px 16px;overflow-x:auto;">
    {% for name, count, pct in data.popular_players[:8] %}
    {% set pl = data.leaderboard.players.get(name, {}) %}
    <div style="flex:0 0 auto;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:12px 16px;min-width:130px;text-align:center;">
      <div style="font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1px;">{{ name.split(' ')[-1] }}</div>
      <div style="font-size:11px;color:rgba(255,255,255,.6);margin-top:2px;">{{ name.split(' ')[0] }}</div>
      <div class="sc {% if pl.get('score',0) < 0 %}under{% elif pl.get('score',0) == 0 %}even{% else %}over{% endif %}" style="font-size:22px;margin:6px 0 4px;">{% if pl.get('score',0) > 0 %}+{% endif %}{{ pl.get('score',0) if pl.get('score',0) != 0 else 'E' }}</div>
      <div style="font-size:10px;color:#ffd700;font-weight:600;">{{ count }} picks</div>
      <div style="font-size:9px;color:rgba(255,255,255,.3);margin-top:2px;">{{ pct }}% of entries</div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<div class="grid-2">

<!-- LEFT: Tipping Leaderboard -->
<div>
{% if data.punters %}
<div class="card">
  <div class="card-title">Tipping Leaderboard <div style="display:flex;align-items:center;gap:10px;">{% if data.leaderboard.last_updated %}<span id="lastUpdated" data-ts="{{ data.leaderboard.last_updated }}Z" style="font-size:10px;color:rgba(255,255,255,.3);font-family:'Inter',sans-serif;font-weight:400;"></span>{% endif %}<span class="badge">{{ data.total_entries }} entries</span></div></div>
  <div class="search">
    <input type="text" id="searchInput" placeholder="Search punter or player name..." onkeyup="filterTable()">
  </div>
  <div class="scroll-table" style="max-height:800px;overflow-x:auto;">
  <table id="leaderboardTable" style="min-width:700px;">
    <thead>
      <tr>
        <th style="width:32px">#</th>
        <th style="width:36px" class="c"></th>
        <th>Punter</th>
        <th class="c">Score</th>
        <th>Pool 1</th>
        <th>Pool 2</th>
        <th>Pool 3</th>
        <th>Pool 4</th>
        <th>Pool 5</th>
        <th class="r">Payout</th>
      </tr>
    </thead>
    <tbody>
    {% for p in data.punters %}
    <tr class="{% if p.payout > 0 %}payout-row{% endif %}" data-search="{{ p.name|lower }} {% for pl in p.players %}{{ pl.name|lower }} {% endfor %}">
      <td><span class="pos-num">{{ p.position }}</span></td>
      <td class="c" style="font-size:11px;font-weight:700;">{% if p.mover > 0 %}<span style="color:#4ade80;">&#9650;{{ p.mover }}</span>{% elif p.mover < 0 %}<span style="color:#f87171;">&#9660;{{ p.mover|abs }}</span>{% else %}<span style="color:rgba(255,255,255,.25);">-</span>{% endif %}</td>
      <td class="punter">{{ p.name }}</td>
      <td class="c"><span class="sc {% if p.total < 0 %}under{% elif p.total == 0 %}even{% else %}over{% endif %}" style="font-size:14px;">{% if p.total > 0 %}+{% endif %}{{ p.total if p.total != 0 else 'E' }}</span></td>
      {% for pl in p.players %}
      <td class="pick-cell">
        <div class="pick-name {% if pl.cut %}cut-txt{% elif pl.capped %}cap-txt{% endif %}">{{ pl.name.split(' ')[-1] }}</div>
        <div class="pick-score sc {% if pl.score < 0 %}under{% elif pl.score == 0 %}even{% else %}over{% endif %}">{% if pl.score > 0 %}+{% endif %}{{ pl.score if pl.score != 0 else 'E' }}{% if pl.capped %}*{% endif %}{% if pl.cut %} CUT{% endif %}</div>
      </td>
      {% endfor %}
      {% for _ in range(5 - p.players|length) %}<td class="pick-cell">-</td>{% endfor %}
      <td class="r money">{% if p.payout > 0 %}<strong>${{ "{:,.0f}".format(p.payout) }}</strong>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</div>
{% else %}
<div class="card">
  <div class="empty">
    <p style="font-size:16px;">No picks loaded yet</p>
    <p style="font-size:12px;">Add entries to picks.json</p>
  </div>
</div>
{% endif %}
</div>

<!-- RIGHT: Tournament Leaderboard -->
<div>
<div class="card">
  <div class="card-title">Tournament Leaderboard <span class="badge">{{ data.tournament_lb|length }} players</span></div>
  <div class="scroll-table" style="max-height:800px;">
  <table>
    <thead>
      <tr>
        <th style="width:28px">Pos</th>
        <th>Player</th>
        <th class="c">Score</th>
        <th class="c">Thru</th>
        <th class="r hide-mobile">Today</th>
        <th class="r hide-mobile">Rounds</th>
      </tr>
    </thead>
    <tbody>
    {% for pl in data.tournament_lb %}
    <tr class="{% if pl.cut %}tlb-cut-row{% endif %}">
      <td><span class="tlb-pos">{{ pl.position or '-' }}</span></td>
      <td>
        <div style="white-space:nowrap;display:flex;align-items:center;gap:5px;">
          {% if pl.flag_url %}<img src="{{ pl.flag_url }}" alt="{{ pl.country }}" title="{{ pl.country }}" style="width:16px;height:11px;border-radius:1px;object-fit:cover;flex-shrink:0;">{% endif %}<span class="tlb-name">{{ pl.name }}</span>{% set pc = data.picked_by_norm.get(pl.name|lower, data.picked_by.get(pl.name, [])) %}{% if pc|length > 0 %}<span class="tlb-picked">{{ pc|length }} picks</span>{% endif %}
        </div>
      </td>
      <td class="c"><span class="sc {% if pl.score < 0 %}under{% elif pl.score == 0 %}even{% else %}over{% endif %}">{% if pl.score > 0 %}+{% endif %}{{ pl.score if pl.score != 0 else 'E' }}{% if pl.cut %} CUT{% endif %}</span></td>
      <td class="c tlb-thru">{% if pl.thru and pl.thru > 0 and pl.thru < 18 %}{{ pl.thru }}{% elif pl.thru == 18 or pl.thru == 0 %}F{% else %}-{% endif %}</td>
      <td class="r">{% if pl.today and pl.today not in ('-', '') %}<span class="sc {% if pl.today.lstrip().startswith('-') %}under{% elif pl.today == 'E' %}even{% else %}over{% endif %}" style="font-size:11px;">{{ pl.today }}</span>{% else %}<span style="color:rgba(255,255,255,.15);">-</span>{% endif %}</td>
      <td class="r tlb-rounds">{{ pl.rounds|join(' / ') if pl.rounds else '-' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</div>
</div>

</div><!-- grid-2 -->

<!-- Odds & Player Pools Table -->
{% if data.odds %}
<div class="card" style="margin-top:20px;">
  <div class="card-title">Player Pools & Odds <span class="badge">Sportsbet</span></div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0;border-top:1px solid rgba(255,255,255,.06);">
    {% set pools = {1: 'Pool 1 (Top 14)', 2: 'Pool 2 (15-34)', 3: 'Pool 3 (35-64)', 4: 'Pool 4 (65-90)'} %}
    {% for pool_num in [1,2,3,4] %}
    <div style="border-right:1px solid rgba(255,255,255,.04);{% if pool_num > 2 %}border-top:1px solid rgba(255,255,255,.04);{% endif %}">
      <div style="padding:10px 14px;font-size:11px;font-weight:700;color:#ffd700;background:rgba(255,215,0,.04);border-bottom:1px solid rgba(255,255,255,.06);font-family:'Playfair Display',serif;letter-spacing:.5px;">
        {{ pools[pool_num] }}
        <span style="float:right;font-family:'Inter',sans-serif;font-size:9px;color:rgba(255,255,255,.3);font-weight:500;">
          {% if pool_num == 1 %}Pick 2{% elif pool_num == 2 %}Pick 1{% elif pool_num == 3 %}Pick 1{% else %}Pick 1{% endif %}
        </span>
      </div>
      <table style="font-size:11px;">
        <thead>
          <tr>
            <th style="padding:6px 10px;">Player</th>
            <th class="r" style="padding:6px 10px;">Odds</th>
            <th class="r" style="padding:6px 10px;">Score</th>
            <th class="r" style="padding:6px 10px;">Picked</th>
          </tr>
        </thead>
        <tbody>
        {% for o in data.odds if o.pool == pool_num %}
        <tr{% if o.cut %} style="opacity:.4"{% endif %}>
          <td style="padding:5px 10px;font-weight:500;">{{ o.player }}</td>
          <td class="r" style="padding:5px 10px;color:rgba(255,255,255,.5);">${{ "%.0f"|format(o.odds) }}</td>
          <td class="r" style="padding:5px 10px;">
            {% if o.tournament_score is not none %}
            <span class="sc {% if o.tournament_score < 0 %}under{% elif o.tournament_score == 0 %}even{% else %}over{% endif %}">
              {% if o.tournament_score > 0 %}+{% endif %}{{ o.tournament_score if o.tournament_score != 0 else 'E' }}{% if o.cut %} CUT{% endif %}
            </span>
            {% else %}-{% endif %}
          </td>
          <td class="r" style="padding:5px 10px;">
            {% if o.pick_count > 0 %}
            <span style="color:#ffd700;font-weight:600;">{{ o.pick_count }}</span>
            <span style="color:rgba(255,255,255,.25);font-size:9px;">({{ ((o.pick_count / data.total_entries) * 100)|round(1) }}%)</span>
            {% else %}<span style="color:rgba(255,255,255,.15);">0</span>{% endif %}
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

</div>

<div class="footer">
  Auto-refreshes every 2 minutes &middot; Scores from ESPN &middot; * Score capped at cut line
  {% if data.leaderboard.last_updated %}&middot; Last updated: {{ data.leaderboard.last_updated }}{% endif %}
</div>

<script>
function filterTable(){
  const q=document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('#leaderboardTable tbody tr').forEach(tr=>{
    tr.style.display=(tr.dataset.search||'').includes(q)?'':'none';
  });
}

// Relative time for "Updated X ago"
(function(){
  var el=document.getElementById('lastUpdated');
  if(!el)return;
  var ts=new Date(el.dataset.ts).getTime();
  function update(){
    var diff=Math.floor((Date.now()-ts)/1000);
    var txt;
    if(diff<60) txt=diff+'s ago';
    else if(diff<3600) txt=Math.floor(diff/60)+'m ago';
    else txt=Math.floor(diff/3600)+'h '+Math.floor((diff%3600)/60)+'m ago';
    el.textContent='Updated '+txt;
  }
  update();
  setInterval(update,10000);
})();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    data = calculate_standings()
    return render_template_string(TEMPLATE, data=data)


@app.route("/api/standings")
def api_standings():
    return calculate_standings()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8060))
    print(f"\n  Masters Tipping — Live Dashboard")
    print(f"  Open: http://localhost:{port}\n")
    app.run(debug=False, host="0.0.0.0", port=port)
