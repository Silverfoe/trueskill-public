# trueskill_api.py
# See: https://trueskill.org for documentation (Everything but the API is based from there)
# Docs: https://docs.google.com/document/d/1CbBLLqyYKtuPC5mxcyPSOP7CdqY8LLGMmciP24ALHzU/edit?usp=sharing
# -------------------------------------------------------------------------
# Requirements:
#   pip install flask requests trueskill
# Run:
#   python trueskill_api.py
# It will listen on http://127.0.0.1:5000
#
# IMPORTANT:
#   - Do NOT name this file "trueskill.py" (it WILL shadow the trueskill library).
#   - Set your TBA key in the environment: export TBA_AUTH_KEY="your_real_key"

from flask import Flask, request, jsonify
import os
import requests
import math
import re
import trueskill

app = Flask(__name__)

# -------------------------
# Config
# -------------------------
TBA_AUTH_KEY = ("TBA_AUTH_KEY")
TEAM_RATINGS = {}  # in-memory store { "frc####": trueskill.Rating }

# Validate event key format like "2025nyrr"
EVENT_KEY_RE = re.compile(r"^\d{4}[a-z0-9]+$", re.IGNORECASE)

# -------------------------
# Helpers
# -------------------------
def validate_event_key(event_key: str) -> None:
    if not event_key or not isinstance(event_key, str):
        raise ValueError("event_key is required.")
    if not EVENT_KEY_RE.match(event_key.strip()):
        raise ValueError("Invalid event_key. Use full key with year, e.g., 2025nyrr.")

def tba_get_event_matches_simple(event_key: str):
    """Fetch matches for an event from TBA."""
    if not TBA_AUTH_KEY:
        raise RuntimeError("TBA_AUTH_KEY is not set.")
    url = f"https://www.thebluealliance.com/api/v3/event/{event_key}/matches/simple"
    headers = {"X-TBA-Auth-Key": TBA_AUTH_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"TBA request error: {e}")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        raise RuntimeError(f"TBA HTTP {resp.status_code}: {snippet}")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("TBA response was not valid JSON.")
    if not isinstance(data, list):
        raise RuntimeError("Unexpected TBA payload (expected a JSON list).")
    return data

def init_rating_if_missing(team_key: str):
    """Ensure a team has a Rating object."""
    if team_key not in TEAM_RATINGS:
        TEAM_RATINGS[team_key] = trueskill.Rating()  # default mu=25, sigma≈25/3

def update_from_one_match(teams_red, teams_blue, score_red, score_blue):
    """Apply a single match result into TrueSkill (3v3 or any other size)."""
    if teams_red is None or teams_blue is None:
        return
    if score_red is None or score_blue is None:
        return  # skip unplayed/invalid rows

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
    """TrueSkill win probability for alliance1 vs alliance2 (per trueskill.org)."""
    env = trueskill.global_env()
    a1 = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in (alliance1_keys or [])]
    a2 = [TEAM_RATINGS.setdefault(t, trueskill.Rating()) for t in (alliance2_keys or [])]
    delta_mu = sum(r.mu for r in a1) - sum(r.mu for r in a2)
    sum_sigma2 = sum(r.sigma ** 2 for r in a1 + a2)
    size = len(a1) + len(a2)
    denom = math.sqrt(size * (env.beta ** 2) + sum_sigma2)
    return float(env.cdf(delta_mu / denom))

# -------------------------
# API Endpoints
# -------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "teams_indexed": len(TEAM_RATINGS)})

@app.post("/update")
def update_rankings():
    """Rebuild ratings from TBA for a given event."""
    try:
        data = request.get_json(silent=True) or {}
        event_key = str(data.get("event_key", "")).strip().lower()
        validate_event_key(event_key)

        matches = tba_get_event_matches_simple(event_key)
        if not matches:
            return jsonify({"status": "no matches found", "event_key": event_key}), 404

        # Reset all ratings
        TEAM_RATINGS.clear()

        # Ensure ratings for all teams seen
        for m in matches:
            alli = m.get("alliances", {})
            red = (alli.get("red") or {})
            blu = (alli.get("blue") or {})
            for t in (red.get("team_keys") or []) + (blu.get("team_keys") or []):
                init_rating_if_missing(t)

        # Apply all results in order
        for m in matches:
            alli = m.get("alliances", {})
            red = (alli.get("red") or {})
            blu = (alli.get("blue") or {})
            update_from_one_match(
                red.get("team_keys") or [],
                blu.get("team_keys") or [],
                red.get("score"),
                blu.get("score"),
            )

        return jsonify({
            "status": "rankings updated",
            "teams_indexed": len(TEAM_RATINGS),
            "event_key": event_key
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
    # Localhost only, as requested
    app.run(host="127.0.0.1", port=5000, debug=False)
