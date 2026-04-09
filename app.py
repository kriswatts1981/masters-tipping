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

# Round-end standings cache — persisted to disk so it survives restarts
ROUND_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "round_standings.json")

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
        try:
            with open(PICKS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            print(f"ERROR loading picks.json: {e}")
    return []


def load_odds():
    if os.path.exists(ODDS_FILE):
        with open(ODDS_FILE) as f:
            return json.load(f)
    return []


def load_round_standings():
    """Load previously saved round-end standings from disk."""
    if os.path.exists(ROUND_CACHE_FILE):
        with open(ROUND_CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_round_standings(round_num, positions):
    """Save end-of-round positions to disk. positions = {punter_name: position}"""
    data = load_round_standings()
    data[str(round_num)] = positions
    with open(ROUND_CACHE_FILE, "w") as f:
        json.dump(data, f)


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
    tid = cfg['tournament_id']

    # Fetch BOTH endpoints: scoreboard has correct score strings,
    # leaderboard has thru/position/tee times
    scoreboard_scores = {}  # name -> score string ("-5", "+3", "E")
    try:
        sb_url = f"https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard?tournamentId={tid}"
        sb_r = requests.get(sb_url, timeout=10)
        sb_r.raise_for_status()
        sb_data = sb_r.json()
        for ev in sb_data.get("events", []):
            for comp in ev.get("competitions", []):
                for c in comp.get("competitors", []):
                    name = c.get("athlete", {}).get("displayName", "")
                    sc = c.get("score", "E")
                    if name and isinstance(sc, str):
                        scoreboard_scores[name] = sc
    except Exception as e:
        print(f"ESPN scoreboard fetch (secondary): {e}")

    # Primary: leaderboard endpoint (has thru, position, tee times)
    url = f"{cfg['espn_api']}?tournamentId={tid}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"ESPN API error: {e}")
        _cache["ts"] = time.time() - CACHE_TTL + 30
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

    # First pass: parse all player data
    par = 72  # Augusta National par
    player_data_list = []
    for c in competitors:
        athlete = c.get("athlete", {})
        name = athlete.get("displayName", "Unknown")
        flag_data = athlete.get("flag", {})
        country = flag_data.get("alt", "")
        flag_url = flag_data.get("href", "")
        # Prefer scoreboard score (correct relative-to-par string) over leaderboard
        # Leaderboard displayValue is broken during live play (shows "E" for everyone)
        if name in scoreboard_scores:
            score_str = scoreboard_scores[name]
        else:
            score_raw = c.get("score", "E")
            if isinstance(score_raw, dict):
                score_str = score_raw.get("displayValue", "E")
            else:
                score_str = str(score_raw) if score_raw else "E"
        if score_str in ("-", "", None):
            score_str = "E"
        position = c.get("status", {}).get("position", {}).get("displayName", "")
        player_status = c.get("status", {}).get("type", {}).get("name", "")
        thru = c.get("status", {}).get("thru", 0)
        today_score = c.get("status", {}).get("todayScore", "")
        tee_time = c.get("status", {}).get("teeTime", "")
        tee_detail = c.get("status", {}).get("detail", "")

        # Parse total score relative to par
        if score_str == "E":
            total_score = 0
        else:
            try:
                total_score = int(score_str)
            except (ValueError, TypeError):
                total_score = 0

        # Round scores — stroke totals (e.g., 68, 72)
        linescores = c.get("linescores", [])
        rounds_strokes = []
        for ls in linescores[:4]:  # cap at 4 rounds
            val = ls.get("value", 0)
            if val and val > 50:  # sanity check — real round scores are 60-85
                rounds_strokes.append(int(val))

        # Detect cut: player has fewer completed rounds than expected
        # ESPN gives cut players only 2 rounds of data
        rounds_played = len(rounds_strokes)
        is_cut = (current_round >= 3 or status_type in ("STATUS_FINAL", "STATUS_PLAY_COMPLETE")) and rounds_played <= 2 and current_round > 2
        # Also check ESPN's status field as backup
        if player_status == "cut":
            is_cut = True

        player_data_list.append({
            "name": name, "score_str": score_str, "total_score": total_score,
            "rounds_strokes": rounds_strokes, "rounds_played": rounds_played,
            "is_cut": is_cut, "position": position, "player_status": player_status,
            "thru": thru, "today": today_score, "country": country, "flag_url": flag_url,
            "tee_time": tee_time, "tee_detail": tee_detail,
        })

    # Determine cut line from R1+R2 scores (relative to par)
    # The cut is made after R2 — cut line = worst R2 total among players who made the cut
    cut_line = None
    if current_round >= 3 or status_type in ("STATUS_FINAL", "STATUS_PLAY_COMPLETE"):
        made_cut_r2 = []
        for p in player_data_list:
            if p["is_cut"]:
                continue
            if len(p["rounds_strokes"]) >= 2:
                r2_total = sum(p["rounds_strokes"][:2]) - (par * 2)
            else:
                r2_total = p["total_score"]
            made_cut_r2.append(r2_total)
        if made_cut_r2:
            cut_line = max(made_cut_r2)

    # Second pass: apply cut rule and build players dict
    players = {}
    for p in player_data_list:
        total_score = p["total_score"]
        is_cut = p["is_cut"]

        # Apply cut rule:
        # - Missed cut: use their R1+R2 score relative to par
        # - Made cut but total score worse than cut line: cap at cut line
        effective_score = total_score
        if is_cut:
            # Missed cut — use R1+R2 relative to par
            if len(p["rounds_strokes"]) >= 2:
                effective_score = sum(p["rounds_strokes"][:2]) - (par * 2)
        elif cut_line is not None and total_score > cut_line:
            # Made the cut but blew out — cap at cut line
            effective_score = cut_line

        name = p["name"]
        players[name] = {
            "name": name,
            "score": total_score,
            "effective_score": effective_score,
            "score_display": p["score_str"],
            "position": p["position"],
            "status": p["player_status"],
            "thru": p["thru"],
            "today": p["today"],
            "rounds": p["rounds_strokes"],
            "cut": is_cut,
            "country": p["country"],
            "flag_url": p["flag_url"],
            "tee_time": p["tee_time"],
            "tee_detail": p["tee_detail"],
            "started": p["thru"] > 0 or p["player_status"] not in ("", "STATUS_SCHEDULED"),
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
                # Unmatched player gets worst score in field as penalty (not free E)
                worst = max((p["score"] for p in players.values()), default=20)
                penalty = max(worst, 10)  # at least +10
                player_details.append({
                    "pick": pick,
                    "name": pick + " (?)",
                    "score": penalty,
                    "actual_score": penalty,
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
    payouts_per_pos = {}
    for pos_num, pct in payout_pcts.items():
        payouts_per_pos[pos_num] = round(prize_pool * pct / 100, 0)

    # Split payouts for ties: if N punters tie at position P,
    # they split the prize money for positions P through P+N-1
    from collections import Counter
    pos_counts = Counter(p["position"] for p in punter_results)
    for p in punter_results:
        pos = p["position"]
        if pos <= cfg["payout_places"]:
            n_tied = pos_counts[pos]
            pool_for_tied = sum(payouts_per_pos.get(pos + i, 0) for i in range(n_tied) if pos + i <= cfg["payout_places"])
            p["payout"] = round(pool_for_tied / n_tied, 0)
        else:
            p["payout"] = 0

    payouts = payouts_per_pos

    # Build sorted tournament leaderboard for display
    # Sort: made-cut players by score first, then cut players by score
    # Sort: started players by (cut, score), then not-started by tee time
    tournament_lb = sorted(players.values(), key=lambda x: (
        not x.get("started", False),  # started first (False < True)
        x["cut"],                      # made cut before missed cut
        x["score"],                    # lowest score first
        x.get("tee_time", ""),         # earliest tee time for non-started
    ))

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

    # Calculate movers: compare current position vs end of previous round
    current_round = lb.get("current_round", 0)
    round_standings = load_round_standings()

    # Build current positions dict
    current_positions = {p["name"]: p["position"] for p in punter_results}

    # Save current standings as the latest round snapshot
    # (will be "previous round" next time a new round starts)
    if current_round > 0:
        save_round_standings(current_round, current_positions)

    # Find the previous round's standings to compare against
    prev_round = str(current_round - 1) if current_round > 1 else None
    prev_positions = round_standings.get(prev_round, {}) if prev_round else {}

    # If no previous round data, fall back to per-round score calculation
    if not prev_positions:
        # Calculate standings using scores through previous round only
        prev_round_num = max(current_round - 1, 1)
        prev_results = []
        for p in punter_results:
            prev_total = 0
            for pl in p["players"]:
                rounds = pl.get("rounds", [])
                # Sum only rounds up to prev_round_num
                for r_idx in range(min(prev_round_num, len(rounds))):
                    prev_total += rounds[r_idx]
                if not rounds:
                    prev_total += pl["score"]
            prev_results.append({"name": p["name"], "prev_total": prev_total})

        prev_results.sort(key=lambda x: x["prev_total"])
        prev_pos = 1
        for i, r in enumerate(prev_results):
            if i > 0 and r["prev_total"] == prev_results[i - 1]["prev_total"]:
                prev_positions[r["name"]] = prev_positions[prev_results[i - 1]["name"]]
            else:
                prev_positions[r["name"]] = prev_pos
            prev_pos = i + 2

    # Assign mover field (positive = moved up, negative = moved down)
    mover_label = f"vs R{current_round - 1}" if current_round > 1 else ""
    for p in punter_results:
        prev_rank = prev_positions.get(p["name"], p["position"])
        p["mover"] = prev_rank - p["position"]

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
            # Use ESPN name to resolve pick counts when odds name differs
            if o["pick_count"] == 0:
                o["pick_count"] = len(picked_by.get(matched["name"], []))
        else:
            o["tournament_score"] = None
            o["position"] = ""
            o["cut"] = False

    # Generate fun facts
    fun_facts = []
    if punter_results:
        # Most popular player
        if popular_players:
            top_p = popular_players[0]
            fun_facts.append(f"{top_p[0]} is the most popular pick — selected by {top_p[1]:,} entries ({top_p[2]}%)")

        # Least popular picked player (min picks > 0)
        least = [p for p in popular_players if p[1] > 0]
        if least:
            bottom = least[-1]
            fun_facts.append(f"{bottom[0]} is the hipster pick — only {bottom[1]} entries backed them")

        # Only show mover/bubble facts once tournament is underway
        has_scores = any(p["total"] != 0 for p in punter_results)
        if has_scores:
            # Biggest mover up today
            best_mover = max(punter_results, key=lambda x: x.get("mover", 0))
            if best_mover["mover"] > 0:
                fun_facts.append(f"{best_mover['name']} is today's biggest mover — climbed {best_mover['mover']:,} places")

            # Biggest drop
            worst_mover = min(punter_results, key=lambda x: x.get("mover", 0))
            if worst_mover["mover"] < 0:
                fun_facts.append(f"{worst_mover['name']} had the toughest day — dropped {abs(worst_mover['mover']):,} places")

            # Just missed the money (bubble)
            bubble = [p for p in punter_results if p["position"] == cfg["payout_places"] + 1]
            if bubble:
                fun_facts.append(f"{bubble[0]['name']} is the bubble — just missed the money at position {bubble[0]['position']}")

        # Worst total score (only when scores exist)
        if has_scores:
            worst_punter = punter_results[-1]
            fun_facts.append(f"{worst_punter['name']} brings up the rear at +{worst_punter['total']}" if worst_punter["total"] > 0 else f"{worst_punter['name']} is last at {worst_punter['total']}")

        # Best score in a single pick
        best_pick_score = None
        best_pick_name = None
        for p in punter_results[:50]:  # check top 50
            for pl in p["players"]:
                if best_pick_score is None or pl["score"] < best_pick_score:
                    best_pick_score = pl["score"]
                    best_pick_name = pl["name"]

        # Winner's payout
        if has_scores:
            winner = punter_results[0]
            fun_facts.append(f"1st place takes home ${winner['payout']:,.0f} from a ${cfg['buy_in']} entry")
        else:
            pool = total_entries * cfg["buy_in"]
            fun_facts.append(f"${pool:,.0f} prize pool up for grabs from {total_entries:,} x ${cfg['buy_in']} entries")

        # How many unique player combos
        combos = set()
        for p in punter_results:
            combo = tuple(sorted(pl["name"] for pl in p["players"]))
            combos.add(combo)
        fun_facts.append(f"{len(combos):,} unique player combinations across {total_entries:,} entries")

        # Most common duo
        from collections import Counter
        duos = Counter()
        for p in punter_results:
            names = sorted(pl["name"] for pl in p["players"])
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    duos[(names[i], names[j])] += 1
        if duos:
            top_duo, duo_count = duos.most_common(1)[0]
            fun_facts.append(f"{top_duo[0].split(' ')[-1]} + {top_duo[1].split(' ')[-1]} is the most popular combo — picked together {duo_count:,} times")

        # Most popular full 5-player combination
        combo_counter = Counter()
        for p in punter_results:
            combo = tuple(sorted(pl["name"] for pl in p["players"]))
            combo_counter[combo] += 1
        if combo_counter:
            top_combo, top_combo_count = combo_counter.most_common(1)[0]
            if top_combo_count > 1:
                short_names = " / ".join(n.split(" ")[-1] for n in top_combo)
                fun_facts.append(f"{top_combo_count} punters picked the exact same team: {short_names}")

        # Pool-level stats: most popular per pool
        pool_picks = {}
        for p in punter_results:
            for i, pl in enumerate(p["players"]):
                pool_picks.setdefault(i, Counter())[pl["name"]] += 1
        for pool_idx, ctr in sorted(pool_picks.items()):
            top_name, top_ct = ctr.most_common(1)[0]
            pool_label = f"Pool {pool_idx + 1}" if pool_idx < 5 else f"Pool {pool_idx + 1}"
            pct = round(top_ct / total_entries * 100, 1)
            fun_facts.append(f"{top_name.split(' ')[-1]} dominates {pool_label} — chosen by {pct}% of punters")

        # Countdown to tournament (pre-tournament only)
        import datetime
        now_utc = datetime.datetime.utcnow()
        # R1 starts approx 7:40 AM ET = 11:40 UTC on April 9
        r1_start = datetime.datetime(2026, 4, 9, 11, 40, 0)
        if now_utc < r1_start:
            delta = r1_start - now_utc
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            if hours > 0:
                fun_facts.insert(0, f"First tee in {hours}h {mins}m — Round 1 starts Thursday morning at Augusta")
            else:
                fun_facts.insert(0, f"First tee in {mins} minutes!")

        # Average picks per player
        unique_players_picked = len([p for p in popular_players if p[1] > 0])
        fun_facts.append(f"{unique_players_picked} of 91 players in the field were selected by at least one punter")

        # Players nobody picked
        all_in_field = set(p["name"] for p in tournament_lb) if tournament_lb else set()
        all_picked = set(p[0] for p in popular_players if p[1] > 0)
        unpicked = all_in_field - all_picked
        if unpicked and len(unpicked) <= 8:
            fun_facts.append(f"Not backed by anyone: {', '.join(sorted(unpicked))}")
        elif unpicked:
            fun_facts.append(f"{len(unpicked)} players in the field weren't selected by a single punter")

        # Rory stat (defending champ)
        rory_picks = next((p for p in popular_players if "McIlroy" in p[0]), None)
        if rory_picks:
            fun_facts.append(f"Defending champion Rory McIlroy features in {rory_picks[1]:,} teams ({rory_picks[2]}%)")

    # Tournament facts
    if tournament_lb:
        leader = tournament_lb[0]
        if leader.get("started", False):
            fun_facts.append(f"{leader['name']} leads the tournament at {leader['score']}")

        # Leading amateur
        amateurs = [p for p in tournament_lb if "(a)" in p.get("name", "")]
        if amateurs:
            fun_facts.append(f"Leading amateur: {amateurs[0]['name']} at {amateurs[0]['score']}")

        # Best round (lowest completed round — must be 60-85 range for a real 18-hole score)
        best_round = None
        best_round_player = None
        for p in tournament_lb:
            for i, r in enumerate(p.get("rounds", [])):
                if r and 60 <= r <= 85 and (best_round is None or r < best_round):
                    best_round = r
                    best_round_player = f"{p['name']} (R{i+1})"
        if best_round:
            fun_facts.append(f"Low round of the tournament: {best_round} by {best_round_player}")

        # --- Dynamic in-tournament facts (only when scores exist) ---
        has_scores = any(p.get("score", 0) != 0 for p in tournament_lb)
        if has_scores and punter_results:
            # How many punters in the money
            in_money = sum(1 for p in punter_results if p.get("payout", 0) > 0)
            fun_facts.append(f"{in_money:,} punters currently in the money — {total_entries - in_money:,} chasing")

            # Leader's payout
            leader_punter = punter_results[0]
            if leader_punter.get("payout", 0) > 0:
                fun_facts.append(f"{leader_punter['name']} leads and would pocket ${leader_punter['payout']:,.0f} if it ended now")

            # Gap between 1st and last
            gap = punter_results[-1]["total"] - punter_results[0]["total"]
            if gap > 0:
                fun_facts.append(f"{gap} stroke gap between first and last — from {punter_results[0]['total']:+d} to {punter_results[-1]['total']:+d}")

            # How the most popular pick is doing
            if popular_players:
                top_name = popular_players[0][0]
                top_player = next((p for p in tournament_lb if p["name"] == top_name), None)
                if top_player:
                    pos = top_player.get("position", "?")
                    sc = top_player["score"]
                    sc_str = f"{sc:+d}" if sc != 0 else "E"
                    fun_facts.append(f"Fan favourite {top_name} sits at {sc_str} ({pos}) — backed by {popular_players[0][1]:,} punters")

            # Hipster pick performance
            least = [p for p in popular_players if p[1] > 0]
            if least:
                hipster = least[-1]
                hp = next((p for p in tournament_lb if p["name"] == hipster[0]), None)
                if hp and hp["score"] < 0:
                    fun_facts.append(f"Hipster hero: {hipster[0]} ({hipster[1]} picks) is outperforming at {hp['score']:+d}")
                elif hp and hp.get("cut"):
                    fun_facts.append(f"Hipster heartbreak: {hipster[0]} missed the cut — {hipster[1]} punters feeling it")

            # Players under par count
            under_par = sum(1 for p in tournament_lb if p["score"] < 0 and not p.get("cut"))
            over_par = sum(1 for p in tournament_lb if p["score"] > 0 and not p.get("cut"))
            fun_facts.append(f"Augusta is winning: {over_par} players over par vs {under_par} under par")

            # Cut carnage (after R2)
            cut_players = [p for p in tournament_lb if p.get("cut")]
            if cut_players:
                cut_picks_affected = 0
                for pr in punter_results:
                    for pl in pr["players"]:
                        if pl.get("cut"):
                            cut_picks_affected += 1
                            break
                fun_facts.append(f"{len(cut_players)} players missed the cut — affecting {cut_picks_affected:,} punter teams")

            # All 5 picks made the cut
            if cut_players:
                all_made = sum(1 for p in punter_results if not any(pl.get("cut") for pl in p["players"]))
                pct_made = round(all_made / total_entries * 100, 1)
                fun_facts.append(f"{all_made:,} punters ({pct_made}%) have all 5 picks still alive after the cut")

            # Highest scoring single pick in the field
            best_pick_score = None
            best_pick_player = None
            for p in punter_results[:100]:
                for pl in p["players"]:
                    if not pl.get("cut") and (best_pick_score is None or pl["score"] < best_pick_score):
                        best_pick_score = pl["score"]
                        best_pick_player = pl["name"]
            if best_pick_score is not None and best_pick_score < 0:
                bp_count = sum(1 for pp in popular_players if pp[0] == best_pick_player)
                bp_picks = next((pp[1] for pp in popular_players if pp[0] == best_pick_player), 0)
                fun_facts.append(f"{best_pick_player} is the best individual pick at {best_pick_score:+d} — in {bp_picks:,} teams")

            # Worst pick among popular players (top 10 most selected)
            if popular_players:
                worst_pop = None
                worst_pop_score = None
                for pp in popular_players[:10]:
                    tp = next((p for p in tournament_lb if p["name"] == pp[0]), None)
                    if tp and (worst_pop_score is None or tp["score"] > worst_pop_score):
                        worst_pop_score = tp["score"]
                        worst_pop = (pp[0], pp[1], tp["score"])
                if worst_pop and worst_pop[2] > 0:
                    fun_facts.append(f"{worst_pop[0]} is letting down {worst_pop[1]:,} punters at +{worst_pop[2]}")

            # Money line — how much you'd win at each position
            paid_places = cfg.get("payout_places", 20)
            profit_count = sum(1 for p in punter_results if p.get("payout", 0) > cfg["buy_in"])
            fun_facts.append(f"{profit_count:,} punters would profit right now — {total_entries - profit_count:,} would lose their ${cfg['buy_in']}")

            # Closest race for the money
            if paid_places < len(punter_results):
                last_paid = next((p for p in punter_results if p["position"] == paid_places), None)
                first_unpaid = next((p for p in punter_results if p["position"] == paid_places + 1), None)
                if last_paid and first_unpaid:
                    diff = first_unpaid["total"] - last_paid["total"]
                    if diff <= 2:
                        fun_facts.append(f"Battle for the money: only {diff} stroke{'s' if diff != 1 else ''} separating position {paid_places} from {paid_places + 1}")

    return {
        "punters": punter_results,
        "leaderboard": lb,
        "tournament_lb": tournament_lb,
        "picked_by": picked_by,
        "picked_by_norm": {k.lower(): v for k, v in picked_by.items()},
        "popular_players": popular_players,
        "odds": odds,
        "weather": fetch_weather(),
        "mover_label": mover_label,
        "fun_facts": fun_facts,
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
.stats{display:flex;justify-content:center;gap:32px;padding:14px 20px;background:rgba(0,0,0,.25);flex-wrap:wrap;border-bottom:2px solid rgba(255,215,0,.3);}
.stat{text-align:center;}
.stat .val{font-size:24px;color:#ffd700;font-weight:700;font-family:'Playfair Display',serif;}
.stat .lbl{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:rgba(255,255,255,.5);margin-top:2px;}

/* Layout */
.container{max-width:1400px;margin:0 auto;padding:16px 20px;}
.grid-2{display:grid;grid-template-columns:62fr 38fr;gap:12px;overflow:hidden;}
.grid-2>div{display:flex;flex-direction:column;}
.grid-2>div>.card:last-child{flex:1;display:flex;flex-direction:column;}
.grid-2>div>.card:last-child>.scroll-table{flex:1;max-height:none;}
@media(max-width:1600px){.hide-narrow{display:none!important;}}
@media(max-width:1024px){.grid-2{grid-template-columns:1fr;}.hide-narrow{display:table-cell!important;}}
@media(max-width:768px){
  .hero-content{padding:14px 12px 10px;}
  .hero h1{font-size:22px;}
  .hero-logo{gap:10px;}
  .hero-flag-icon{width:28px;height:34px;}
  .hero-flag-icon .flag{width:14px;height:10px;}
  .hero .sub{font-size:12px;}
  .hero-bg::before{display:none;}
  .hero-bg::after{display:none;}
  .scoreboard-strip{padding:6px 4px;gap:2px;justify-content:flex-start;}
  .sb-cell{min-width:48px;padding:4px 3px;}
  .sb-cell .sb-name{font-size:8px;max-width:48px;}
  .sb-cell .sb-score{font-size:13px;}
  .stats{gap:8px 16px;padding:10px 8px;}
  .stat .val{font-size:16px;}
  .stat .lbl{font-size:7px;letter-spacing:.5px;}
  .container{padding:8px;}
  .card{margin-bottom:10px;border-radius:6px;}
  .card-title{padding:8px 10px;font-size:11px;flex-wrap:wrap;gap:4px;}
  .card-title .badge{font-size:8px;padding:2px 6px;}
  .search{padding:6px 8px;}
  .search input{padding:6px 10px;font-size:12px;}
  th{padding:5px 4px;font-size:7px;letter-spacing:.8px;}
  td{padding:5px 4px;font-size:11px;}
  .punter{font-size:11px;white-space:normal!important;width:auto!important;max-width:140px;}
  .pos-num{font-size:10px;min-width:16px;}
  .sc{font-size:11px!important;}
  .scroll-table{max-height:70vh;overflow-y:auto;overflow-x:hidden;}
  /* Tipping table: hide pool columns + payout on mobile */
  .pick-col{display:none!important;}
  .payout-col{display:none!important;}
  #leaderboardTable{min-width:0!important;table-layout:auto;}
  .money{font-size:11px;}
  /* Hide mover column on small screens */
  .mover-col{width:24px;}
  /* Tournament leaderboard — hide Thru on mobile too, just Pos/Player/Score */
  .tlb-name{font-size:11px;}
  .tlb-picked{font-size:7px;padding:1px 3px;}
  .tlb-rounds{font-size:9px;}
  .tlb-thru{font-size:9px;}
  .tlb-pos{font-size:10px;min-width:18px;}
  .hide-thru-mobile{display:none!important;}
  .footer{font-size:9px;padding:12px 8px;}
  /* Hide less important columns on mobile */
  .hide-mobile{display:none!important;}
  .hide-narrow{display:none!important;}
  /* Weather bar stacks */
  .weather-bar{flex-direction:column;gap:4px!important;padding:6px 10px!important;font-size:11px!important;}
  /* Popular players smaller cards */
  .popular-card{min-width:100px!important;padding:8px 10px!important;}
  .popular-card .pop-score{font-size:18px!important;}
  .popular-card .pop-name{font-size:9px!important;}
  .popular-card .pop-first{font-size:10px!important;}
  .popular-card .pop-picks{font-size:9px!important;}
  .popular-card .pop-pct{font-size:8px!important;}
  /* Pools grid stacks */
  .pools-grid{grid-template-columns:1fr!important;}
}

/* Cards */
.card{background:rgba(0,0,0,.2);border-radius:8px;overflow:hidden;margin-bottom:20px;border:1px solid rgba(255,255,255,.08);backdrop-filter:blur(10px);}
.card-title{padding:10px 14px;font-size:14px;font-weight:700;color:#ffd700;border-bottom:1px solid rgba(255,255,255,.08);display:flex;justify-content:space-between;align-items:center;font-family:'Playfair Display',serif;letter-spacing:.5px;}
.card-title .badge{font-size:10px;background:rgba(255,215,0,.12);color:#ffd700;padding:3px 10px;border-radius:12px;font-family:'Inter',sans-serif;font-weight:600;}

/* Tables */
table{width:100%;border-collapse:collapse;}
th{text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:rgba(255,255,255,.4);padding:8px 6px;border-bottom:1px solid rgba(255,255,255,.08);font-weight:600;}
th.r,td.r{text-align:right;}
th.c,td.c{text-align:center;}
td{padding:6px 6px;border-bottom:1px solid rgba(255,255,255,.04);font-size:12px;}
tr:hover{background:rgba(255,255,255,.03);}

/* Scores */
.sc{font-family:'Courier New',monospace;font-weight:700;}
.under{color:#4ade80;}
.even{color:rgba(255,255,255,.8);}
.over{color:#f87171;}
.cut-txt{color:rgba(255,255,255,.3);text-decoration:line-through;}
.cap-txt{color:#fb923c;}

/* Tipping table */
.pos-num{font-weight:700;color:#ffd700;}
.punter{font-weight:600;font-size:13px;white-space:nowrap;width:180px;max-width:180px;}
.payout-row{background:rgba(255,215,0,.04);}
.money{color:#ffd700;font-weight:700;}
.pick-cell{font-size:11px;white-space:nowrap;}
.pick-name{font-weight:500;}
.pick-score{opacity:.65;margin-left:2px;}

/* Tournament leaderboard */
.tlb-pos{font-weight:600;color:rgba(255,255,255,.5);min-width:22px;display:inline-block;font-size:11px;}
.tlb-name{font-weight:500;font-size:11px;}
.tlb-picked{font-size:8px;color:#ffd700;background:rgba(255,215,0,.1);padding:1px 4px;border-radius:3px;margin-left:3px;white-space:nowrap;vertical-align:middle;}
.tlb-rounds{font-size:9px;color:rgba(255,255,255,.4);}
.tlb-cut-row td{opacity:.4;}
.tlb-thru{font-size:10px;color:rgba(255,255,255,.5);}

/* Search */
.search{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.06);}
.search input{width:100%;padding:7px 12px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:#fff;font-size:12px;font-family:'Inter',sans-serif;}
.search input::placeholder{color:rgba(255,255,255,.25);}
.search input:focus{outline:none;border-color:rgba(255,215,0,.3);}

/* Tabs */
.tabs{display:flex;border-bottom:1px solid rgba(255,255,255,.08);}
.tab{padding:10px 18px;font-size:12px;font-weight:600;color:rgba(255,255,255,.4);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;}
.tab:hover{color:rgba(255,255,255,.7);}
.tab.active{color:#ffd700;border-bottom-color:#ffd700;}

/* Favourites */
.fav-star{cursor:pointer;font-size:12px;color:rgba(255,255,255,.15);transition:color .15s;vertical-align:middle;margin-right:2px;}
.fav-star:hover{color:rgba(255,215,0,.5);}
.fav-star.active{color:#ffd700;}
.fav-row{background:rgba(255,215,0,.04)!important;}
.fav-header td{padding:4px 10px!important;background:rgba(255,215,0,.06)!important;border:none!important;font-size:9px;color:rgba(255,215,0,.5);letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid rgba(255,215,0,.1)!important;}
.fav-separator td{padding:4px 10px!important;background:rgba(0,0,0,.15)!important;border:none!important;font-size:9px;color:rgba(255,255,255,.3);letter-spacing:1px;text-transform:uppercase;border-top:1px solid rgba(255,215,0,.1)!important;border-bottom:1px solid rgba(255,215,0,.1)!important;}

/* Fun facts ticker — uses JS to set width for mobile compatibility */
.ticker-wrap{width:100%;overflow:hidden;position:relative;}
.ticker{display:inline-block;white-space:nowrap;-webkit-animation:ticker-scroll 180s linear infinite;animation:ticker-scroll 180s linear infinite;}
.ticker:hover{-webkit-animation-play-state:paused;animation-play-state:paused;}
.ticker-item{display:inline-block;padding:0 28px;font-size:12px;color:rgba(255,255,255,.55);font-family:'Inter',sans-serif;white-space:nowrap;vertical-align:middle;}
.ticker-item::before{content:'\25CF';color:#ffd700;font-size:5px;margin-right:10px;vertical-align:middle;}
@-webkit-keyframes ticker-scroll{from{-webkit-transform:translateX(0)}to{-webkit-transform:translateX(-50%)}}
@keyframes ticker-scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}

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
<div class="weather-bar" style="display:flex;justify-content:space-between;align-items:center;padding:8px 24px;background:rgba(0,0,0,.15);border-bottom:1px solid rgba(255,255,255,.06);flex-wrap:wrap;gap:8px;">
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

<!-- Fun Facts Ticker -->
{% if data.fun_facts %}
<style>
@keyframes ffscroll{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.ff-wrap{background:rgba(0,0,0,.2);border-bottom:1px solid rgba(255,215,0,.1);overflow:hidden;height:28px;position:relative;}
.ff-track{display:flex;white-space:nowrap;animation:ffscroll var(--ff-dur,60s) linear infinite;position:absolute;top:0;left:0;height:100%;align-items:center;}
.ff-track:hover{animation-play-state:paused;}
.ff-item{font-size:12px;color:rgba(255,255,255,.55);font-family:Inter,sans-serif;padding:0 40px;flex-shrink:0;}
.ff-item .ff-dot{color:#ffd700;margin-right:8px;}
</style>
<div class="ff-wrap">
  <div class="ff-track" id="ffTrack">
    {% for f in data.fun_facts %}<div class="ff-item"><span class="ff-dot">&#9679;</span>{{ f }}</div>{% endfor %}
    {% for f in data.fun_facts %}<div class="ff-item"><span class="ff-dot">&#9679;</span>{{ f }}</div>{% endfor %}
  </div>
</div>
<script>
(function(){
  var track=document.getElementById('ffTrack');
  if(!track)return;
  var items=track.children.length/2;
  var dur=Math.max(items*8,50);
  track.style.setProperty('--ff-dur',dur+'s');
})();
</script>
{% endif %}

<div class="container">

<!-- Most Popular Players -->
{% if data.popular_players %}
<div class="card" style="margin-bottom:20px;">
  <div class="card-title">Most Selected Players <span class="badge">who's backing who</span></div>
  <div style="display:flex;gap:8px;padding:12px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch;">
    {% for name, count, pct in data.popular_players[:10] %}
    {% set pl = data.leaderboard.players.get(name, {}) %}
    <div class="popular-card" style="flex:0 0 auto;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:12px 16px;min-width:120px;text-align:center;">
      <div class="pop-name" style="font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1px;">{{ name.split(' ')[-1] }}</div>
      <div class="pop-first" style="font-size:11px;color:rgba(255,255,255,.6);margin-top:2px;">{{ name.split(' ')[0] }}</div>
      <div class="pop-score sc {% if pl.get('score',0) < 0 %}under{% elif pl.get('score',0) == 0 %}even{% else %}over{% endif %}" style="font-size:22px;margin:6px 0 4px;">{% if pl.get('score',0) > 0 %}+{% endif %}{{ pl.get('score',0) if pl.get('score',0) != 0 else 'E' }}</div>
      <div class="pop-picks" style="font-size:10px;color:#ffd700;font-weight:600;">{{ count }} picks</div>
      <div class="pop-pct" style="font-size:9px;color:rgba(255,255,255,.3);margin-top:2px;">{{ pct }}%</div>
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
  <div class="card-title"><span>Tipping Leaderboard</span> <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">{% if data.leaderboard.last_updated %}<span id="lastUpdated" data-ts="{{ data.leaderboard.last_updated }}Z" style="font-size:10px;color:rgba(255,255,255,.3);font-family:'Inter',sans-serif;font-weight:400;"></span>{% endif %}<span class="badge">{{ data.total_entries }}</span></div></div>
  <div class="search" style="position:relative;">
    <input type="text" id="searchInput" placeholder="Search punter name..." onkeyup="filterTable()">
    <span id="searchClear" onclick="document.getElementById('searchInput').value='';filterTable();this.style.display='none';" style="display:none;position:absolute;right:20px;top:50%;transform:translateY(-50%);cursor:pointer;color:rgba(255,255,255,.4);font-size:16px;line-height:1;padding:4px;">&times;</span>
  </div>
  <div class="scroll-table" style="max-height:800px;overflow-x:auto;">
  <table id="leaderboardTable" style="min-width:600px;">
    <thead>
      <tr>
        <th style="width:20px;padding-right:0">#</th>
        <th style="width:28px;padding-left:0" class="c" title="Position change vs previous round">{% if data.mover_label %}{{ data.mover_label }}{% endif %}</th>
        <th>Punter</th>
        <th class="c" style="width:38px">Score</th>
        <th class="pick-col">Pool 1</th>
        <th class="pick-col">Pool 2</th>
        <th class="pick-col">Pool 3</th>
        <th class="pick-col">Pool 4</th>
        <th class="pick-col">Pool 5</th>
        <th class="r payout-col" style="width:60px">Payout</th>
      </tr>
    </thead>
    <tbody>
    {% for p in data.punters %}
    <tr class="{% if p.payout > 0 %}payout-row{% endif %}" data-search="{{ p.name|lower }} {% for pl in p.players %}{{ pl.name|lower }} {% endfor %}" data-punter="{{ p.name }}" data-order="{{ loop.index0 }}">
      <td style="padding-right:0"><span class="pos-num">{{ p.position }}</span></td>
      <td class="c" style="font-size:10px;font-weight:700;padding-left:0;">{% if p.mover > 0 %}<span style="color:#4ade80;">&#9650;{{ p.mover }}</span>{% elif p.mover < 0 %}<span style="color:#f87171;">&#9660;{{ p.mover|abs }}</span>{% else %}<span style="color:rgba(255,255,255,.25);">-</span>{% endif %}</td>
      <td class="punter"><span class="fav-star" onclick="toggleFav(this)" title="Add to favourites">&#9734;</span> {{ p.name }}</td>
      <td class="c"><span class="sc {% if p.total < 0 %}under{% elif p.total == 0 %}even{% else %}over{% endif %}" style="font-size:14px;">{% if p.total > 0 %}+{% endif %}{{ p.total if p.total != 0 else 'E' }}</span></td>
      {% for pl in p.players %}
      <td class="pick-cell pick-col"><span class="pick-name {% if pl.cut %}cut-txt{% elif pl.capped %}cap-txt{% endif %}">{{ pl.name.split(' ')[-1] }}</span> <span class="pick-score sc {% if pl.score < 0 %}under{% elif pl.score == 0 %}even{% else %}over{% endif %}">{% if pl.score > 0 %}+{% endif %}{{ pl.score if pl.score != 0 else 'E' }}{% if pl.capped %}*{% endif %}{% if pl.cut %} CUT{% endif %}</span></td>
      {% endfor %}
      {% for _ in range(5 - p.players|length) %}<td class="pick-cell pick-col">-</td>{% endfor %}
      <td class="r money payout-col">{% if p.payout > 0 %}<strong>${{ "{:,.0f}".format(p.payout) }}</strong>{% endif %}</td>
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
  <div class="search" style="position:relative;">
    <input type="text" id="tlbSearch" placeholder="Search player..." onkeyup="filterTlb()" style="width:100%;padding:8px 12px;background:rgba(0,0,0,.2);border:1px solid rgba(255,255,255,.08);border-radius:6px;color:#e0e8d8;font-size:12px;font-family:Inter,sans-serif;">
  </div>
  <div class="scroll-table" style="max-height:800px;">
  <table>
    <thead>
      <tr>
        <th style="width:26px">Pos</th>
        <th>Player</th>
        <th class="r" style="width:38px">Score</th>
        <th class="c hide-thru-mobile" style="width:24px">Thru</th>
        <th class="r hide-narrow" style="width:28px">Tdy</th>
        <th class="r hide-narrow">Rounds</th>
      </tr>
    </thead>
    <tbody>
    {% set cut_shown = [false] %}
    {% for pl in data.tournament_lb %}
    {% if pl.cut and not cut_shown[0] and data.leaderboard.cut_line is not none %}
    <tr class="tlb-cut-divider">
      <td colspan="6" style="padding:6px 10px;background:rgba(0,0,0,.2);border-top:2px solid rgba(255,100,100,.3);border-bottom:1px solid rgba(255,255,255,.06);font-size:10px;color:rgba(255,255,255,.45);font-style:italic;letter-spacing:.3px;">
        Projected cut at {{ "+" if data.leaderboard.cut_line > 0 }}{{ data.leaderboard.cut_line if data.leaderboard.cut_line != 0 else "E" }} &mdash; players below missed the cut
      </td>
    </tr>
    {% if cut_shown.append(true) %}{% endif %}{% if cut_shown.pop(0) is not none %}{% endif %}
    {% endif %}
    <tr class="{% if pl.cut %}tlb-cut-row{% endif %} tlb-row" data-tlb="{{ pl.name|lower }}">
      <td><span class="tlb-pos">{{ pl.position or '-' }}</span></td>
      <td>
        {% if pl.flag_url %}<img src="{{ pl.flag_url }}" alt="{{ pl.country }}" title="{{ pl.country }}" style="width:14px;height:10px;border-radius:1px;object-fit:cover;vertical-align:middle;margin-right:3px;">{% endif %}<span class="tlb-name">{{ pl.name }}</span>{% set pc = data.picked_by_norm.get(pl.name|lower, data.picked_by.get(pl.name, [])) %}{% if pc|length > 0 %} <span class="tlb-picked">{{ pc|length }}</span>{% endif %}
      </td>
      <td class="r">{% if not pl.started and pl.tee_detail %}<span style="color:rgba(255,255,255,.35);font-size:10px;white-space:nowrap;">{{ pl.tee_detail.replace(' ET','') }}</span>{% else %}<span class="sc {% if pl.score < 0 %}under{% elif pl.score == 0 %}even{% else %}over{% endif %}">{% if pl.score > 0 %}+{% endif %}{{ pl.score if pl.score != 0 else 'E' }}{% if pl.cut %} CUT{% endif %}</span>{% endif %}</td>
      <td class="c tlb-thru hide-thru-mobile">{% if not pl.started %}-{% elif pl.thru and pl.thru > 0 and pl.thru < 18 %}{{ pl.thru }}{% elif pl.thru == 18 or pl.thru == 0 %}F{% else %}-{% endif %}</td>
      <td class="r hide-narrow">{% if pl.today and pl.today not in ('-', '') %}<span class="sc {% if pl.today.lstrip().startswith('-') %}under{% elif pl.today == 'E' %}even{% else %}over{% endif %}" style="font-size:11px;">{{ pl.today }}</span>{% else %}<span style="color:rgba(255,255,255,.15);">-</span>{% endif %}</td>
      <td class="r tlb-rounds hide-narrow" style="white-space:nowrap;font-size:10px;">{{ pl.rounds|join('/') if pl.rounds else '-' }}</td>
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
  <div class="pools-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;border-top:1px solid rgba(255,255,255,.06);padding:8px;">
    {% set pools = {1: 'Pool 1 (Top 14)', 2: 'Pool 2 (15-35)', 3: 'Pool 3 (36-58)', 4: 'Pool 4 (59-91)'} %}
    {% for pool_num in [1,2,3,4] %}
    <div style="border:1px solid rgba(255,255,255,.06);border-radius:6px;overflow:hidden;">
      <div style="padding:10px 14px;font-size:11px;font-weight:700;color:#ffd700;background:rgba(255,215,0,.04);border-bottom:1px solid rgba(255,255,255,.06);font-family:'Playfair Display',serif;letter-spacing:.5px;">
        {{ pools[pool_num] }}
        <span style="float:right;font-family:'Inter',sans-serif;font-size:9px;color:rgba(255,255,255,.3);font-weight:500;">
          {% if pool_num == 1 %}Pick 2{% elif pool_num == 2 %}Pick 1{% elif pool_num == 3 %}Pick 1{% else %}Pick 1{% endif %}
        </span>
      </div>
      <table style="font-size:11px;width:100%;">
        <thead>
          <tr>
            <th style="padding:6px 6px;">Player</th>
            <th class="r" style="padding:6px 2px;width:32px;">Odds</th>
            <th class="r" style="padding:6px 2px;width:34px;">Score</th>
            <th class="r" style="padding:6px 6px;white-space:nowrap;">Picked</th>
          </tr>
        </thead>
        <tbody>
        {% for o in data.odds if o.pool == pool_num %}
        <tr{% if o.cut %} style="opacity:.4"{% endif %}>
          <td style="padding:4px 6px;font-weight:500;">{{ o.player }}</td>
          <td class="r" style="padding:4px 2px;color:rgba(255,255,255,.5);">${{ "%.0f"|format(o.odds) }}</td>
          <td class="r" style="padding:4px 2px;">
            {% if o.tournament_score is not none %}
            <span class="sc {% if o.tournament_score < 0 %}under{% elif o.tournament_score == 0 %}even{% else %}over{% endif %}">
              {% if o.tournament_score > 0 %}+{% endif %}{{ o.tournament_score if o.tournament_score != 0 else 'E' }}{% if o.cut %} CUT{% endif %}
            </span>
            {% else %}-{% endif %}
          </td>
          <td class="r" style="padding:5px 6px;white-space:nowrap;">
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
// Favourites — stored in localStorage
function getFavs(){try{return JSON.parse(localStorage.getItem('masters_favs')||'[]');}catch(e){return[];}}
function saveFavs(f){localStorage.setItem('masters_favs',JSON.stringify(f));}

function toggleFav(el){
  var tr=el.closest('tr');
  var name=tr.dataset.punter;
  var favs=getFavs();
  var idx=favs.indexOf(name);
  if(idx>=0){favs.splice(idx,1);el.classList.remove('active');el.innerHTML='&#9734;';}
  else{favs.push(name);el.classList.add('active');el.innerHTML='&#9733;';}
  saveFavs(favs);
  reorderFavs();
}

function reorderFavs(){
  var tbody=document.querySelector('#leaderboardTable tbody');
  if(!tbody)return;
  var favs=getFavs();
  // Remove old headers/separators
  tbody.querySelectorAll('.fav-separator,.fav-header').forEach(function(el){el.remove();});
  tbody.querySelectorAll('tr').forEach(function(tr){tr.classList.remove('fav-row');});
  if(!favs.length){
    // No favs — restore original order without rebuilding entire DOM
    var all=Array.from(tbody.querySelectorAll('tr[data-punter]'));
    all.sort(function(a,b){return parseInt(a.dataset.order)-parseInt(b.dataset.order);});
    all.forEach(function(r){tbody.appendChild(r);});
    return;
  }
  var cols=tbody.querySelector('tr')?tbody.querySelector('tr').children.length:5;
  var favSet=new Set(favs);
  // Only move fav rows — leave the rest in place for speed
  var rows=Array.from(tbody.querySelectorAll('tr[data-punter]'));
  var favRows=rows.filter(function(tr){return favSet.has(tr.dataset.punter);});
  favRows.sort(function(a,b){return parseInt(a.dataset.order)-parseInt(b.dataset.order);});
  // Build fragment: header + fav rows + separator
  var frag=document.createDocumentFragment();
  var hdr=document.createElement('tr');
  hdr.className='fav-header';
  hdr.innerHTML='<td colspan="'+cols+'">&#9733; Favourites</td>';
  frag.appendChild(hdr);
  favRows.forEach(function(r){r.classList.add('fav-row');frag.appendChild(r);});
  var sep=document.createElement('tr');
  sep.className='fav-separator';
  sep.innerHTML='<td colspan="'+cols+'">All Punters</td>';
  frag.appendChild(sep);
  // Insert before the first remaining data row
  var first=tbody.querySelector('tr[data-punter]');
  if(first){tbody.insertBefore(frag,first);}else{tbody.appendChild(frag);}
}

// Init: mark saved favs and reorder on page load
(function(){
  var favs=getFavs();
  if(!favs.length)return;
  document.querySelectorAll('#leaderboardTable tbody tr[data-punter]').forEach(function(tr){
    if(favs.indexOf(tr.dataset.punter)>=0){
      var star=tr.querySelector('.fav-star');
      if(star){star.classList.add('active');star.innerHTML='&#9733;';}
    }
  });
  reorderFavs();
})();

function filterTable(){
  const q=document.getElementById('searchInput').value.toLowerCase();
  document.getElementById('searchClear').style.display=q?'block':'none';
  document.querySelectorAll('#leaderboardTable tbody tr').forEach(tr=>{
    if(tr.classList.contains('fav-separator')){tr.style.display=q?'none':'';return;}
    tr.style.display=(tr.dataset.search||'').includes(q)?'':'none';
  });
}

function filterTlb(){
  const q=document.getElementById('tlbSearch').value.toLowerCase();
  document.querySelectorAll('.tlb-row').forEach(tr=>{
    tr.style.display=(tr.dataset.tlb||'').includes(q)?'':'none';
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


@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
