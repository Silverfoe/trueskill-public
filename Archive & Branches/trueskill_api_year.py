# trueskill_api_year.py
# Local-only Flask API for TrueSkill predictions (year-wide aggregation).
# ----------------------------------------------------------------------
# Usage:
#   export TBA_AUTH_KEY="your_real_tba_key"
#   python trueskill_api_year.py
#   # Health
#   curl -s http://127.0.0.1:5000/health
#   # Build from a whole YEAR
#   curl -s -X POST http://127.0.0.1:5000/update -H "Content-Type: application/json" -d '{"year": 2025}'
#   # (Still supports single event if you prefer)
#   curl -s -X POST http://127.0.0.1:5000/update -H "Content-Type: application/json" -d '{"event_key":"2025nyrr"}'
#
# Endpoints (same as your single-event API):
#   GET  /health
#   POST /update          (now accepts {"year": 2025} OR {"event_key": "YYYYxxxxx"})
#   POST /push_results
#   GET  /predict_team?team=frc####
#   POST /predict_match
#   POST /predict_batch
#
# Notes:
# - This server aggregates ALL event matches in a season when {"year": <YYYY>} is given.
# - Ratings are kept in-memory only (same as your current build).
# - Bound to localhost only, as requested.

from flask import Flask, request, jsonify
import os
import re
import time
import math
import requests
import trueskill

app = Flask(__name__)

# -------------------------
# Config
# -------------------------
TBA_AUTH_KEY = ("TBA_API_KEY")
TEAM_RATINGS = {}  # { "frc####": trueskill.Rating }

EVENT_KEY_RE = re.compile(r"^\d{4}[a-z0-9]+$", re.IGNORECASE)
YEAR_RE = re.compile(r"^\d{4}$")

# polite pause between TBA calls (helps avoid rate-limit bursts)
REQUEST_SLEEP_SECONDS = float(os.environ.get("TBA_REQ_SLEEP", "0.10"))

# -------------------------
# Helpers (robust)
# -------------------------
def require_tba_key():
    if not TBA_AUTH_KEY:
        raise RuntimeError("TBA_AUTH_KEY is not set. Export it: export TBA_AUTH_KEY=...")

def validate_event_key_or_year(payload: dict) -> dict:
    """
    Accepts either:
      {"year": 2025}
    or
      {"event_key": "2025nyrr"}
    Returns a dict: {"mode": "year", "year": 2025} OR {"mode": "event", "event_key": "2025nyrr"}
    """
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object.")
    if "year" in payload and str(payload["year"]).strip():
        y = str(payload["year"]).strip()
        if not YEAR_RE.match(y):
            raise ValueError('Invalid "year". Use 4 digits, e.g., 2025.')
        return {"mode": "year", "year": int(y)}
    if "event_key" in payload and str(payload["event_key"]).strip():
        ev = str(payload["event_key"]).strip().lower()
        # allow either a normal event key or a bare year (if someone sends "2025" here)
        if YEAR_RE.match(ev):
            return {"mode": "year", "year": int(ev)}
        if not EVENT_KEY_RE.match(ev):
            raise ValueError('Invalid "event_key". Use full key with year, e.g., "2025nyrr".')
        return {"mode": "event", "event_key": ev}
    raise ValueError('Provide {"year": YYYY} or {"event_key": "YYYYxxxxx"}.')

def tba_get_json(url: str) -> any: # type: ignore
    require_tba_key()
    headers = {"X-TBA-Auth-Key": TBA_AUTH_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"TBA request error: {e}")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        raise RuntimeError(f"TBA HTTP {resp.status_code}: {snippet}")
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError("TBA response was not valid JSON.")

def tba_get_events_for_year(year: int) -> list:
    url = f"https://www.thebluealliance.com/api/v3/events/{year}"
    data = tba_get_json(url)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected payload from TBA /events/{year} (expected list).")
    return data

def tba_get_event_matches_simple(event_key: str) -> list:
    url = f"https://www.thebluealliance.com/api/v3/event/{event_key}/matches/simple"
    data = tba_get_json(url)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected payload from TBA /event/{event_key}/matches/simple (expected list).")
    return data

def init_rating_if_missing(team_key: str):
    if team_key not in TEAM_RATINGS:
        TEAM_RATINGS[team_key] = trueskill.Rating()  # default mu=25, sigma≈25/3

def is_valid_score(x):
    # TBA uses -1 for unplayed; treat None or <0 as invalid/unplayed.
    return isinstance(x, int) and x >= 0

def comp_level_order(comp_level: str) -> int:
    # deterministic processing order (practice -> quals -> playoffs)
    order = {"pr": 0, "qm": 1, "ef": 2, "qf": 3, "sf": 4, "f": 5}
    return order.get((comp_level or "").lower(), 9)

def update_from_one_match(teams_red, teams_blue, score_red, score_blue):
    if teams_red is None or teams_blue is None:
        return
    if not (is_valid_score(score_red) and is_valid_score(score_blue)):
        return  # skip unplayed/invalid

    red = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in teams_red]
    blu = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in teams_blue]

    if score_red > score_blue:
        ranks = [0, 1]
    elif score_blue > score_red:
        ranks = [1, 0]
    else:
        ranks = [0, 0]  # tie

    new_red, new_blu = trueskill.rate([red, blu], ranks=ranks)
    for t, r in zip(teams_red, new_red):
        TEAM_RATINGS[t] = r
    for t, r in zip(teams_blue, new_blu):
        TEAM_RATINGS[t] = r

def alliance_win_probability(alliance1_keys, alliance2_keys) -> float:
    env = trueskill.global_env()
    a1 = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in (alliance1_keys or [])]
    a2 = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in (alliance2_keys or [])]
    delta_mu = sum(r.mu for r in a1) - sum(r.mu for r in a2)
    sum_sigma2 = sum(r.sigma ** 2 for r in a1 + a2)
    size = len(a1) + len(a2)
    denom = math.sqrt(size * (env.beta ** 2) + sum_sigma2)
    return float(env.cdf(delta_mu / denom))

def sort_matches_for_processing(matches: list) -> list:
    # Try to process in chronological-ish, then by level/set/match
    def key(m):
        # "actual_time" preferred, then "time"; some may be None
        ts = m.get("actual_time") or m.get("time") or 0
        cl = comp_level_order(m.get("comp_level"))
        return (int(ts or 0), cl, int(m.get("set_number") or 0), int(m.get("match_number") or 0))
    try:
        return sorted(matches, key=key)
    except Exception:
        return matches  # fall back

# -------------------------
# API Endpoints (same set)
# -------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "teams_indexed": len(TEAM_RATINGS)})

@app.post("/update")
def update_rankings():
    """
    Build ratings from:
      - whole season: {"year": 2025}
      - or single event: {"event_key": "2025nyrr"}
    """
    try:
        payload = request.get_json(silent=True) or {}
        mode = validate_event_key_or_year(payload)

        TEAM_RATINGS.clear()

        if mode["mode"] == "event":
            # single event path (kept for compatibility)
            event_key = mode["event_key"]
            matches = tba_get_event_matches_simple(event_key)
            if not matches:
                return jsonify({"status": "no matches found", "event_key": event_key}), 404

            # Ensure all observed teams are initialized
            for m in matches:
                alli = m.get("alliances", {})
                red = (alli.get("red") or {})
                blu = (alli.get("blue") or {})
                for t in (red.get("team_keys") or []) + (blu.get("team_keys") or []):
                    init_rating_if_missing(t)

            # Process in (rough) chronological order
            for m in sort_matches_for_processing(matches):
                alli = m.get("alliances", {})
                red = (alli.get("red") or {})
                blu = (alli.get("blue") or {})
                update_from_one_match(
                    red.get("team_keys") or [],
                    blu.get("team_keys") or [],
                    red.get("score"),
                    blu.get("score"),
                )
            return jsonify({"status": "rankings updated", "teams_indexed": len(TEAM_RATINGS), "event_key": event_key})

        # year-wide path
        year = mode["year"]
        events = tba_get_events_for_year(year)
        if not events:
            return jsonify({"status": "no events found", "year": year}), 404

        # Iterate through all events; aggregate every played match into one global rating table
        events_processed = 0
        matches_seen = 0

        for ev in events:
            event_key = ev.get("key")
            if not event_key or not isinstance(event_key, str):
                continue

            # Fetch matches for this event
            try:
                matches = tba_get_event_matches_simple(event_key)
            except Exception:
                # Skip problematic events but keep going
                continue

            if not matches:
                time.sleep(REQUEST_SLEEP_SECONDS)
                continue

            # Initialize ratings for teams we see
            for m in matches:
                alli = m.get("alliances", {})
                red = (alli.get("red") or {})
                blu = (alli.get("blue") or {})
                for t in (red.get("team_keys") or []) + (blu.get("team_keys") or []):
                    init_rating_if_missing(t)

            # Process event matches (chronological-ish)
            for m in sort_matches_for_processing(matches):
                alli = m.get("alliances", {})
                red = (alli.get("red") or {})
                blu = (alli.get("blue") or {})
                update_from_one_match(
                    red.get("team_keys") or [],
                    blu.get("team_keys") or [],
                    red.get("score"),
                    blu.get("score"),
                )
                matches_seen += 1

            events_processed += 1
            # brief pause between events
            time.sleep(REQUEST_SLEEP_SECONDS)

        if events_processed == 0:
            return jsonify({"status": "no events processed", "year": year}), 404

        return jsonify({
            "status": "rankings updated",
            "teams_indexed": len(TEAM_RATINGS),
            "year": year,
            "events_processed": events_processed,
            "matches_seen": matches_seen
        })

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/push_results")
def push_results():
    """Incrementally apply match results (no TBA call)."""
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, list):
            return jsonify({"error": "Expected a JSON array of match results"}), 400

        applied = 0
        for item in payload:
            teams1 = (item.get("teams1") or [])
            teams2 = (item.get("teams2") or [])
            s1 = item.get("score1", None)
            s2 = item.get("score2", None)
            if not teams1 or not teams2:
                continue
            update_from_one_match(teams1, teams2, s1, s2)
            applied += 1

        return jsonify({"status": "results incorporated", "applied": applied})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/predict_team")
def predict_team():
    """Return mu/sigma for a given team (404 if unseen)."""
    team = request.args.get("team", "").strip().lower()
    if not team:
        return jsonify({"error": "Missing ?team=frc####"}), 400
    rating = TEAM_RATINGS.get(team)
    if rating is None:
        return jsonify({"error": "Team not found or no ratings computed"}), 404
    return jsonify({"team": team, "mu": rating.mu, "sigma": rating.sigma})

@app.post("/predict_match")
def predict_match():
    """Win probability for one matchup."""
    try:
        data = request.get_json(silent=True) or {}
        t1 = data.get("teams1") or []
        t2 = data.get("teams2") or []
        if not t1 or not t2:
            return jsonify({"error": "teams1 and teams2 are required"}), 400
        p1 = alliance_win_probability(t1, t2)
        return jsonify({"team1_win_prob": p1, "team2_win_prob": 1.0 - p1})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/predict_batch")
def predict_batch():
    """Batch win probabilities."""
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, list):
            return jsonify({"error": "Expected a JSON array of match specs"}), 400
        out = []
        for item in payload:
            t1 = item.get("teams1") or []
            t2 = item.get("teams2") or []
            if not t1 or not t2:
                out.append({"error": "Missing teams1 or teams2"})
                continue
            p1 = alliance_win_probability(t1, t2)
            out.append({"teams1": t1, "teams2": t2, "team1_win_prob": p1, "team2_win_prob": 1.0 - p1})
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
