"""
    By: Jacob Wyrozbebski for FIRST Teams Across the Globe
    My GitHub: https://github.com/Silverfoe

    Licensed under GNU GPLv3 or later. See LICENSE file for details.

    A Flask-based API service to manage and predict team rankings using the TrueSkill algorithm.
    Integrates with The Blue Alliance API to fetch match data for ranking updates.
    Supports saving/loading rankings to/from JSON files, and provides endpoints for health checks,
    ranking updates, match predictions, and leaderboard retrieval.

    Please set the TBA API key via the TBA_AUTH_KEY environment variable or hardcode it in the code.
    Optionally set TRUESKILL_DATA_PATH environment variable to specify the JSON file path for saving/loading data.

    For better performance in production, consider using a WSGI server like Gunicorn or uWSGI to run this Flask app.

    Additionally, you may want to tweak the TrueSkill hyperparameters (mu, sigma, beta, tau, draw_probability)

    Please note: This code is not the latest version and random bugs may exist, "new" versions will be added periodically. Use at your own risk.

    Trueskill is the property of Microsoft Corporation. This code uses the 'trueskill' Python package, which is open source, however,
    it is not premitted to use the Trueskill name in derivative works without permission from Microsoft.
    
    This code is provided as-is without warranty of any kind.
"""

from flask import Flask, request, jsonify, abort # TODO: Add abort in case of critical failures
from flask_cors import CORS
import os
import requests
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict
import json
import trueskill

app = Flask(__name__)
# Enable CORS for all routes
CORS(app)

# Global TrueSkill environment and in-memory ratings
env = trueskill.TrueSkill(draw_probability=0.0)
TEAM_RATINGS = {}  # Maps team key (e.g. "frc3173") to trueskill.Rating object

# Blue Alliance API key (set via environment variable or hardcoded here)
TBA_AUTH_KEY = os.environ.get("TBA_AUTH_KEY", "HARD_CODED_API_KEY_IF_DESIRED")

##### JSON Saving Configuration #####
DATA_PATH = os.environ.get("TRUESKILL_DATA_PATH", "trueskill_data.json")
LAST_EVENT_KEY = None
LAST_YEAR = None

#### Helpers ####

def get_team_rating(team_key):
    """Get the TrueSkill Rating for a team, initializing to default if not present."""
    k = str(team_key).strip().lower()
    if k not in TEAM_RATINGS:
        TEAM_RATINGS[k] = env.create_rating(mu=25, sigma=(25/3))
    return TEAM_RATINGS[k]

def team_confidence_from_sigma(sigma: float, env: trueskill.TrueSkill) -> float:
    """
    Confidence in a team's rating based on reduction of uncertainty vs prior.
    Returns a percentage in [0, 100].
    """
    sigma0 = float(env.sigma)
    if sigma0 <= 0:
        return 0.0
    frac = 1.0 - (float(sigma) / sigma0) ** 2
    frac = max(0.0, min(1.0, frac))
    return 100.0 * frac

def _serialize_team_entry(team_key: str, rating: trueskill.Rating) -> dict:
    mu = float(rating.mu)
    sigma = float(rating.sigma)
    return {
        "team_key": team_key,
        "mu": mu,
        "sigma": sigma,
        "conservative_mu_3sigma": mu - 3.0 * sigma,
        "confidence_percent": round(team_confidence_from_sigma(sigma, env), 2),
    }

def _build_export_payload() -> dict:
    teams = [_serialize_team_entry(k, v) for k, v in sorted(TEAM_RATINGS.items(), key=lambda kv: kv[0])]
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "The Blue Alliance (processed locally)",
            "env": {
                "mu": float(env.mu),
                "sigma": float(env.sigma),
                "beta": float(env.beta),
                "tau": float(env.tau),
                "draw_probability": float(env.draw_probability),
            },
            "context": {
                "event_key": LAST_EVENT_KEY,
                "year": LAST_YEAR,
                "teams_indexed": len(TEAM_RATINGS),
            },
        },
        "teams": teams,
    }

def _save_trueskill_json(path: str = DATA_PATH) -> dict:
    payload = _build_export_payload()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload

def _load_trueskill_json(path: str = DATA_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _apply_json_to_memory(payload: dict, use_env_from_json: bool = False) -> int:
    global env, LAST_EVENT_KEY, LAST_YEAR
    if use_env_from_json:
        try:
            meta_env = (payload.get("meta") or {}).get("env") or {}
            mu = float(meta_env.get("mu", env.mu))
            sigma = float(meta_env.get("sigma", env.sigma))
            beta = float(meta_env.get("beta", env.beta))
            tau = float(meta_env.get("tau", env.tau))
            draw_probability = float(meta_env.get("draw_probability", env.draw_probability))
            env = trueskill.TrueSkill(mu=mu, sigma=sigma, beta=beta, tau=tau, draw_probability=draw_probability)
        except Exception:
            pass
    TEAM_RATINGS.clear()
    teams = payload.get("teams", []) or []
    for entry in teams:
        key_raw = entry.get("team_key", "")
        if key_raw is None:
            continue
        key = str(key_raw).strip().lower()
        mu = entry.get("mu")
        sigma = entry.get("sigma")
        if not key or mu is None or sigma is None:
            continue
        TEAM_RATINGS[key] = env.create_rating(mu=float(mu), sigma=float(sigma))
    meta_ctx = (payload.get("meta") or {}).get("context") or {}
    LAST_EVENT_KEY = meta_ctx.get("event_key")
    y = meta_ctx.get("year")
    try:
        LAST_YEAR = int(y) if y is not None and str(y).isdigit() else y
    except Exception:
        LAST_YEAR = y
    return len(TEAM_RATINGS)

def _count_teams_in_payload(payload: dict) -> int:
    teams = payload.get("teams", [])
    return len(teams) if isinstance(teams, list) else 0

###### The API ######

@app.get("/health")
def health():
    return jsonify({"ok": True, "teams_indexed": len(TEAM_RATINGS)})

@app.route('/update', methods=['POST'])
def update_ratings():
    """Rebuild ratings from TBA match data for an event or an entire year."""
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "No JSON body provided"}), 400
    event_key = data.get('event_key')
    year = data.get('year')
    if (event_key and year) or (not event_key and not year):
        return jsonify({"error": "Provide either 'event_key' or 'year'"}), 400
    if not TBA_AUTH_KEY:
        return jsonify({"error": "TBA API key not configured"}), 500
    matches = []
    try:
        if event_key:
            url = f"https://www.thebluealliance.com/api/v3/event/{event_key}/matches/simple"
            resp = requests.get(url, headers={"X-TBA-Auth-Key": TBA_AUTH_KEY})
            if resp.status_code != 200:
                return jsonify({"error": f"TBA API request failed (status {resp.status_code}) for event {event_key}"}), 500
            matches = resp.json()
        else:
            year_int = int(year)
            url = f"https://www.thebluealliance.com/api/v3/events/{year_int}/simple"
            resp = requests.get(url, headers={"X-TBA-Auth-Key": TBA_AUTH_KEY})
            if resp.status_code != 200:
                return jsonify({"error": f"TBA API request failed (status {resp.status_code}) for year {year_int}"}), 500
            events_list = resp.json()
            if not isinstance(events_list, list):
                return jsonify({"error": f"Unexpected response for events {year_int}"}), 500
            for ev in events_list:
                ev_key = ev.get('key')
                if not ev_key:
                    continue
                url_matches = f"https://www.thebluealliance.com/api/v3/event/{ev_key}/matches/simple"
                resp2 = requests.get(url_matches, headers={"X-TBA-Auth-Key": TBA_AUTH_KEY})
                if resp2.status_code == 200:
                    ev_matches = resp2.json()
                    if isinstance(ev_matches, list):
                        matches.extend(ev_matches)
                time.sleep(0.1)
    except Exception as e:
        return jsonify({"error": f"TBA fetch failed: {e}"}), 500
    TEAM_RATINGS.clear()
    try:
        matches.sort(key=lambda m: (m.get('actual_time') or m.get('time') or 0))
    except Exception:
        pass
    for match in matches:
        alliances = match.get('alliances')
        if not alliances:
            continue
        red_alliance = alliances.get('red', {})
        blue_alliance = alliances.get('blue', {})
        teams1 = red_alliance.get('team_keys', [])
        teams2 = blue_alliance.get('team_keys', [])
        score1 = red_alliance.get('score')
        score2 = blue_alliance.get('score')
        if score1 is None or score2 is None or score1 < 0 or score2 < 0:
            continue
        if score1 > score2:
            ranks = [0, 1]
        elif score2 > score1:
            ranks = [1, 0]
        else:
            ranks = [0, 0]
        ratings1 = [get_team_rating(t) for t in teams1]
        ratings2 = [get_team_rating(t) for t in teams2]
        [new_ratings1, new_ratings2] = env.rate([ratings1, ratings2], ranks=ranks)
        for t, new_r in zip(teams1, new_ratings1):
            TEAM_RATINGS[t] = new_r
        for t, new_r in zip(teams2, new_ratings2):
            TEAM_RATINGS[t] = new_r
        global LAST_EVENT_KEY, LAST_YEAR
        LAST_EVENT_KEY = event_key if event_key else None
        LAST_YEAR = int(year) if year else None
    result: Dict[str, Any] = {"status": "rankings updated"}
    if event_key:
        result["event_key"] = event_key
    if year:
        result["year"] = year
    result["teams_indexed"] = len(TEAM_RATINGS)
    return jsonify(result), 200

@app.post("/push_results")
def push_results():
    """Apply additional match results (provided by client) to update ratings incrementally."""
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "No JSON body provided"}), 400
    if not isinstance(data, list):
        return jsonify({"error": "Request body must be a JSON list of match results"}), 400
    applied_count = 0
    for match in data:
        teams1 = match.get("teams1")
        teams2 = match.get("teams2")
        score1 = match.get("score1")
        score2 = match.get("score2")
        if not teams1 or not teams2 or score1 is None or score2 is None:
            continue
        if score1 > score2:
            ranks = [0, 1]
        elif score2 > score1:
            ranks = [1, 0]
        else:
            ranks = [0, 0]
        ratings1 = [get_team_rating(t) for t in teams1]
        ratings2 = [get_team_rating(t) for t in teams2]
        [new_ratings1, new_ratings2] = env.rate([ratings1, ratings2], ranks=ranks)
        for t, new_r in zip(teams1, new_ratings1):
            TEAM_RATINGS[t] = new_r
        for t, new_r in zip(teams2, new_ratings2):
            TEAM_RATINGS[t] = new_r
        applied_count += 1
    return jsonify({"status": "results incorporated", "applied": applied_count}), 200

@app.get("/predict_team")
def predict_team():
    """
    Return the current TrueSkill rating for a team, plus a single confidence % derived from sigma using: confidence_percent = 100 * (1 - Φ(sigma)).
    """
    team_key = request.args.get('team')
    if not team_key:
        return jsonify({"error": "Missing team parameter"}), 400
    k = str(team_key).strip().lower()
    if k not in TEAM_RATINGS:
        return jsonify({"error": "Team not found"}), 404
    rating = TEAM_RATINGS[k]
    mu = float(rating.mu)
    sigma = float(rating.sigma)
    confidence_percent = round(team_confidence_from_sigma(sigma, env), 2)
    return jsonify({
        "team": k,
        "mu": mu,
        "sigma": sigma,
        "conservative_mu_3sigma": mu - 3.0 * sigma,
        "confidence_percent": confidence_percent
    }), 200

@app.post("/predict_match")
def predict_match():
    """
    Predict win probability for a matchup between two alliances.
    Request JSON: { "teams1": [...], "teams2": [...] }
    Returns win probabilities for alliance1 and alliance2.
    """
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "No JSON body provided"}), 400
    teams1 = data.get("teams1") or []
    teams2 = data.get("teams2") or []
    if not teams1 or not teams2:
        return jsonify({"error": "teams1 and teams2 must be provided"}), 400
    ratings1 = [get_team_rating(t) for t in teams1]
    ratings2 = [get_team_rating(t) for t in teams2]
    mu1 = sum(r.mu for r in ratings1)
    mu2 = sum(r.mu for r in ratings2)
    sigma_sq_sum = sum((r.sigma ** 2) for r in (ratings1 + ratings2))
    N = len(ratings1) + len(ratings2)
    delta_mu = mu1 - mu2
    beta = env.beta
    denom = math.sqrt(N * (beta ** 2) + sigma_sq_sum)
    win_prob = float(env.cdf(delta_mu / denom)) if denom != 0 else 0.5
    prediction_conf = abs(2.0 * win_prob - 1.0) * 100.0
    return jsonify({
        "team1_win_prob": win_prob,
        "team2_win_prob": 1.0 - win_prob,
        "prediction_confidence_percent": round(prediction_conf, 2)
    }), 200

@app.route('/predict_batch', methods=['POST'])
def predict_batch():
    """Predict win probabilities for multiple matchups in one request."""
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "No JSON body provided"}), 400
    if not isinstance(data, list):
        return jsonify({"error": "Request body must be a JSON list"}), 400
    results = []
    for match in data:
        teams1 = match.get("teams1") or []
        teams2 = match.get("teams2") or []
        if not teams1 or not teams2:
            results.append({"error": "teams1/teams2 missing"})
            continue
        ratings1 = [get_team_rating(t) for t in teams1]
        ratings2 = [get_team_rating(t) for t in teams2]
        mu1 = sum(r.mu for r in ratings1)
        mu2 = sum(r.mu for r in ratings2)
        sigma_sq_sum = sum((r.sigma ** 2) for r in (ratings1 + ratings2))
        N = len(ratings1) + len(ratings2)
        delta_mu = mu1 - mu2
        beta = env.beta
        denom = math.sqrt(N * (beta ** 2) + sigma_sq_sum)
        win_prob = float(env.cdf(delta_mu / denom)) if denom != 0 else 0.5
        results.append({
            "teams1": teams1,
            "teams2": teams2,
            "team1_win_prob": win_prob,
            "team2_win_prob": 1.0 - win_prob
        })
    return jsonify(results), 200

@app.post("/recalculate")
def recalculate_values():
    """
    Recompute derived values for all teams and re-save trueskill_data.json.
    Optional body: {"source": "json"} to reload TEAM_RATINGS from file first.
    """
    body = request.get_json(silent=True) or {}
    source = str(body.get("source", "memory")).lower()
    try:
        if source == "json":
            payload = _load_trueskill_json(DATA_PATH)
            json_count = _count_teams_in_payload(payload)
            loaded = _apply_json_to_memory(payload)
            count_for_response = json_count
        else:
            count_for_response = len(TEAM_RATINGS)
        saved = _save_trueskill_json(DATA_PATH)
        saved_count = _count_teams_in_payload(saved)
        return jsonify({
            "status": "recalculated",
            "source": source,
            "teams_indexed": count_for_response,
            "file": DATA_PATH,
            "saved_teams_indexed": saved_count,
            "env": saved.get("meta", {}).get("env", {}),
            "context": saved.get("meta", {}).get("context", {})
        }), 200
    except FileNotFoundError:
        return jsonify({"error": f"No data file found at {DATA_PATH}. Run /upload_data first."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Corrupt JSON in {DATA_PATH}: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Recalculate failed: {e}"}), 500

@app.route('/upload_data', methods=['POST'])
def upload_data():
    """Persist current team data to trueskill_data.json and return a summary."""
    try:
        _save_trueskill_json(DATA_PATH)
        return jsonify({
            "status": "saved",
            "file": DATA_PATH,
            "teams_indexed": len(TEAM_RATINGS)
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed to write {DATA_PATH}: {e}"}), 500

@app.route("/load_data", methods=['POST'])
def load_data_from_json():
    """
    Load ratings from trueskill_data.json into memory so the API can use them for predictions.
    Body (optional):
      { "path": "custom/path/to/trueskill_data.json", "use_env_from_json": true }
    """
    body = request.get_json(silent=True) or {}
    path = body.get("path") or DATA_PATH
    use_env_from_json = bool(body.get("use_env_from_json", True))
    try:
        payload = _load_trueskill_json(path)
        loaded = _apply_json_to_memory(payload, use_env_from_json=use_env_from_json)
        return jsonify({
            "status": "loaded",
            "file": path,
            "use_env_from_json": use_env_from_json,
            "teams_indexed": loaded,
            "context": {"event_key": LAST_EVENT_KEY, "year": LAST_YEAR}
        }), 200
    except FileNotFoundError:
        return jsonify({"error": f"No data file found at {path}. Run /upload_data first."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Corrupt JSON in {path}: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to load data from {path}: {e}"}), 500

@app.route('/leaderboard', methods=['GET'])
def get_leaderboard():
    """
    Return a sorted list of all teams in memory with their rating information.
    Sorted by conservative rating (mu - 3*sigma) in descending order.
    """
    if not TEAM_RATINGS:
        return jsonify({"teams": [], "teams_indexed": 0}), 200
    # Serialize all team ratings to dicts
    teams_data = [_serialize_team_entry(team, rating) for team, rating in TEAM_RATINGS.items()]
    # Sort teams by conservative_mu_3sigma (descending)
    teams_data.sort(key=lambda entry: entry.get("conservative_mu_3sigma", 0), reverse=True)
    return jsonify({"teams": teams_data, "teams_indexed": len(teams_data)}), 200


## Run the Code! ##
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
