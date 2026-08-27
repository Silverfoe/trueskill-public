"""
By: Jacob Wyrozebski
For FIRST Team 3173

TrueSkill is the exclusive property of Microsoft Corporation.

Used under the terms of the TrueSkill license:
Microsoft permits only Xbox Live games or non-commercial projects to use TrueSkill(TM).
If your project is commercial, you should find another rating system.

This TrueSkill project is opened under the BSD license but the
TrueSkill(TM) brand is not. 

Some code snippets taken from The Blue Alliance and Stack Overflow
"""

import math
import asyncio
import asyncpg
from datetime import datetime, timezone, timedelta
from contextlib import suppress
from typing import Dict, Any, Optional, cast
import json
import os
import uuid
import logging
import random
from time import monotonic
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import trueskill
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

# Initialize FastAPI app
app = FastAPI(title="FIRST Team 3173's TrueSkill API", description="FastAPI backend for TrueSkill ratings with concurrency and live updates for Knightwatch FRC scouting app.", version="4.0.4")

# Enable CORS for all origins (for front-end requests)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TrueSkill environment with tuned hyperparameters for FRC
MU = 25.0
SIGMA = 9.0                 # slightly higher uncertainty to allow quick adjustment
BETA = 3.0                  # lower beta to make skill differences translate to clearer win probabilities
TAU = 0.04                  # smaller dynamics factor (skills drift slowly over a season)
DRAW_PROB = 1.0 / 1250.0    # 1/1250 chance of draw (~0.0008)
env = trueskill.TrueSkill(mu=MU, sigma=SIGMA, beta=BETA, tau=TAU, draw_probability=DRAW_PROB)

# In-memory ratings: team key -> trueskill.Rating
TEAM_RATINGS: Dict[str, trueskill.Rating] = {}

# Async lock to synchronize updates to TEAM_RATINGS and database
update_lock = asyncio.Lock()

# WebSocket clients set
clients: set[WebSocket] = set()

# Last context for data updates (set when /update is called)
LAST_EVENT_KEY: Optional[str] = None
LAST_YEAR: Optional[int] = None

# Keep track of processed matches to avoid duplicate processing (especially for live background updates)
processed_match_keys: set[str] = set()
logger = logging.getLogger(__name__)
TRANSIENT_TBA_STATUS_CODES = {429, 500, 502, 503, 504}
ACTIVE_EVENTS_CACHE: dict[int, dict[str, Any]] = {}

def _find_dotenv(start: str) -> Optional[str]:
    p = os.path.abspath(start)
    while True:
        candidate = os.path.join(p, ".env")
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _load_dotenv_defaults() -> None:
    """
    Load `.env` values as defaults if not already exported.
    This keeps local development zero-config for both TBA and Postgres settings.
    """
    dotenv = _find_dotenv(os.path.dirname(__file__))
    if not dotenv:
        return
    try:
        with open(dotenv, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        # Missing or unreadable .env should not block startup.
        pass


_load_dotenv_defaults()

# Prefer dedicated TBA key, fallback to VITE key. .env defaults already loaded above.
TBA_AUTH_KEY = (os.environ.get("TBA_AUTH_KEY") or os.environ.get("VITE_TBA_API_KEY") or "").strip()

def get_team_rating(team_key: str) -> trueskill.Rating:
    """Get or initialize a team's TrueSkill Rating."""
    k = str(team_key).strip().lower()
    if k not in TEAM_RATINGS:
        TEAM_RATINGS[k] = env.create_rating()  # use default mu, sigma from env if team not already present
    return TEAM_RATINGS[k]

def team_confidence_from_sigma(sigma: float) -> float:
    """Compute confidence percentage from sigma (how much uncertainty has reduced)."""
    sigma0 = float(env.sigma)
    if sigma0 <= 0:
        return 0.0
    frac = 1.0 - (float(sigma) / sigma0) ** 2
    frac = max(0.0, min(1.0, frac))
    return 100.0 * frac

def serialize_team_entry(team_key: str, rating: trueskill.Rating) -> Dict[str, Any]:
    """Convert a team rating to dict including conservative rating and confidence."""
    mu_val = float(rating.mu)
    sigma_val = float(rating.sigma)
    return {
        "team_key": team_key,
        "mu": mu_val,
        "sigma": sigma_val,
        "conservative_mu_3sigma": mu_val - 3.0 * sigma_val,
        "confidence_percent": round(team_confidence_from_sigma(sigma_val), 2)
    }

def build_leaderboard_data() -> Dict[str, Any]:
    """Build leaderboard JSON data: all teams sorted by conservative rating."""
    teams_data = [serialize_team_entry(team, rating) for team, rating in TEAM_RATINGS.items()]
    teams_data.sort(key=lambda entry: entry["conservative_mu_3sigma"], reverse=True)
    return {"teams": teams_data, "teams_indexed": len(teams_data)}

async def broadcast_leaderboard():
    """Broadcast updated leaderboard to all connected WebSocket clients."""
    if not clients:
        return
    message = {
        "type": "leaderboard_update",
        "data": build_leaderboard_data()
    }
    # Send to each client; remove any that disconnect
    disconnected = []
    for ws in clients:
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        clients.discard(ws)


async def _tba_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    max_attempts: int = 6,
    base_delay: float = 0.35,
) -> httpx.Response:
    """
    Retry transient TBA failures (network errors / 429 / 5xx) with exponential backoff so that TBA dosen't slime us out.
    Returns the last response if retries are exhausted.
    """
    last_error: Optional[Exception] = None
    last_response: Optional[httpx.Response] = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            last_error = exc
        else:
            if response.status_code not in TRANSIENT_TBA_STATUS_CODES:
                return response
            last_response = response

        if attempt < max_attempts:
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"TBA request failed without response for {url}")

@app.get("/health")
async def health():
    """Health check endpoint with DB connectivity status."""
    pool = getattr(app.state, "db_pool", None)
    db_connected = False
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_connected = True
        except Exception:
            db_connected = False
    return {
        "ok": db_connected,
        "db_connected": db_connected,
        "teams_indexed": len(TEAM_RATINGS),
    }

@app.post("/update")
async def update_ratings(request: Request):
    """
    Rebuild ratings from TBA match data for an event or an entire year.
    Body: {"event_key": "..."} or {"year": ...}.
    """

    data = await request.json()
    event_key = data.get("event_key")
    year = data.get("year")

    # Validate input: exactly one of event_key/year
    if (event_key and year) or (not event_key and not year):
        return JSONResponse({"error": "Provide either 'event_key' or 'year' (exactly one)."}, status_code=400)

    year_int = None
    if year is not None:
        try:
            year_int = int(year)
        except Exception:
            return JSONResponse({"error": "Invalid 'year' value; must be an integer"}, status_code=400)

    # IMPORTANT: do NOT default to "TBA_AUTH_KEY" (that breaks auth silently)
    tba_key = (os.environ.get("TBA_AUTH_KEY") or os.environ.get("VITE_TBA_API_KEY") or "").strip()
    if not tba_key:
        return JSONResponse({"error": "TBA API key not configured (set TBA_AUTH_KEY)."}, status_code=500)

    user_agent = os.environ.get("TBA_USER_AGENT", "Team3173-TrueSkillAPI/4.0.4")

    run_started_at = monotonic()
    matches: list[dict[str, Any]] = []
    fetched_event_keys: list[str] = []
    year_team_keys: set[str] = set()
    year_events_fetched = 0

    async with update_lock:
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                http2=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10, keepalive_expiry=30.0),
            ) as client:
                base_headers = {
                    "X-TBA-Auth-Key": tba_key,
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                }

                if event_key:
                    # ---- Single event ----
                    resource = f"event/{event_key}/matches/simple"

                    # ETag lookup (optional)
                    etag = None
                    async with app.state.db_pool.acquire() as conn:
                        row = await conn.fetchrow("SELECT etag FROM etag_cache WHERE resource=$1", resource)
                        if row:
                            etag = row["etag"]

                    headers = dict(base_headers)
                    if etag:
                        headers["If-None-Match"] = etag

                    url = f"https://www.thebluealliance.com/api/v3/event/{event_key}/matches/simple"
                    resp = await client.get(url, headers=headers)

                    if resp.status_code in (401, 403):
                        detail = (resp.text or "")[:300]
                        return JSONResponse(
                            {
                                "error": f"TBA auth failed ({resp.status_code}). Check TBA_AUTH_KEY and User-Agent.",
                                "hint": "Set env: TBA_AUTH_KEY=... and optionally TBA_USER_AGENT=team/app/version",
                                "detail": detail,
                            },
                            status_code=500,
                        )

                    if resp.status_code == 304:
                        return {"status": "no new data", "event_key": event_key}

                    if resp.status_code != 200:
                        detail = (resp.text or "")[:300]
                        return JSONResponse(
                            {"error": f"TBA API error {resp.status_code}", "detail": detail},
                            status_code=500,
                        )

                    payload = resp.json()
                    matches = payload if isinstance(payload, list) else []

                    new_etag = resp.headers.get("ETag")
                    if new_etag:
                        async with app.state.db_pool.acquire() as conn:
                            await conn.execute(
                                "INSERT INTO etag_cache(resource, etag) VALUES($1, $2) "
                                "ON CONFLICT(resource) DO UPDATE SET etag=excluded.etag",
                                resource,
                                new_etag,
                            )

                else:
                    # ---- Whole year: all events, all teams ----
                    url_events = f"https://www.thebluealliance.com/api/v3/events/{year_int}/simple"
                    resp = await _tba_get_with_retry(client, url_events, base_headers)

                    if resp.status_code in (401, 403):
                        detail = (resp.text or "")[:300]
                        return JSONResponse(
                            {
                                "error": f"TBA auth failed ({resp.status_code}). Check TBA_AUTH_KEY and User-Agent.",
                                "detail": detail,
                            },
                            status_code=500,
                        )

                    if resp.status_code != 200:
                        detail = (resp.text or "")[:300]
                        return JSONResponse(
                            {"error": f"TBA API error {resp.status_code}", "detail": detail},
                            status_code=500,
                        )

                    events_payload = resp.json()
                    if not isinstance(events_payload, list):
                        events_payload = []

                    seen_event_keys: set[str] = set()
                    for ev in events_payload:
                        if not isinstance(ev, dict):
                            continue
                        k = str(ev.get("key", "")).strip()
                        if not k or k in seen_event_keys:
                            continue
                        seen_event_keys.add(k)
                        fetched_event_keys.append(k)

                    etag_updates: list[tuple[str, str]] = []

                    for ev_key in fetched_event_keys:
                        teams_resource = f"event/{ev_key}/teams/keys"
                        teams_url = f"https://www.thebluealliance.com/api/v3/event/{ev_key}/teams/keys"
                        teams_resp = await _tba_get_with_retry(client, teams_url, base_headers)

                        if teams_resp.status_code in (401, 403):
                            detail = (teams_resp.text or "")[:300]
                            return JSONResponse(
                                {
                                    "error": f"TBA auth failed ({teams_resp.status_code}) while fetching teams for event {ev_key}.",
                                    "detail": detail,
                                },
                                status_code=500,
                            )

                        if teams_resp.status_code != 200:
                            detail = (teams_resp.text or "")[:300]
                            return JSONResponse(
                                {
                                    "error": f"TBA API error {teams_resp.status_code} while fetching teams for event {ev_key}",
                                    "detail": detail,
                                },
                                status_code=500,
                            )

                        teams_payload = teams_resp.json()
                        if not isinstance(teams_payload, list):
                            return JSONResponse(
                                {"error": f"TBA teams payload malformed for event {ev_key}"},
                                status_code=500,
                            )

                        for team_key in teams_payload:
                            normalized = str(team_key).strip().lower()
                            if normalized:
                                year_team_keys.add(normalized)

                        teams_etag = teams_resp.headers.get("ETag")
                        if teams_etag:
                            etag_updates.append((teams_resource, teams_etag))

                        matches_resource = f"event/{ev_key}/matches/simple"
                        matches_url = f"https://www.thebluealliance.com/api/v3/event/{ev_key}/matches/simple"
                        matches_resp = await _tba_get_with_retry(client, matches_url, base_headers)

                        if matches_resp.status_code in (401, 403):
                            detail = (matches_resp.text or "")[:300]
                            return JSONResponse(
                                {
                                    "error": f"TBA auth failed ({matches_resp.status_code}) while fetching matches for event {ev_key}.",
                                    "detail": detail,
                                },
                                status_code=500,
                            )

                        if matches_resp.status_code != 200:
                            detail = (matches_resp.text or "")[:300]
                            return JSONResponse(
                                {
                                    "error": f"TBA API error {matches_resp.status_code} while fetching matches for event {ev_key}",
                                    "detail": detail,
                                },
                                status_code=500,
                            )

                        matches_payload = matches_resp.json()
                        if not isinstance(matches_payload, list):
                            return JSONResponse(
                                {"error": f"TBA matches payload malformed for event {ev_key}"},
                                status_code=500,
                            )

                        for match in matches_payload:
                            if not isinstance(match, dict):
                                continue
                            if not match.get("event_key"):
                                match["event_key"] = ev_key
                            matches.append(match)

                        matches_etag = matches_resp.headers.get("ETag")
                        if matches_etag:
                            etag_updates.append((matches_resource, matches_etag))

                        year_events_fetched += 1

                    if etag_updates:
                        async with app.state.db_pool.acquire() as conn:
                            await conn.executemany(
                                "INSERT INTO etag_cache(resource, etag) VALUES($1, $2) "
                                "ON CONFLICT(resource) DO UPDATE SET etag=excluded.etag",
                                etag_updates,
                            )

        except Exception as e:
            return JSONResponse({"error": f"TBA fetch failed: {e}"}, status_code=500)

        # Sort for stable rating progression
        try:
            if event_key:
                matches.sort(key=lambda m: m.get("actual_time") or m.get("time") or 0)
            else:
                matches.sort(
                    key=lambda m: (
                        m.get("actual_time") or m.get("time") or 0,
                        str(m.get("event_key") or ""),
                        str(m.get("key") or ""),
                    )
                )
        except Exception:
            pass

        # ---- Prefetch existing match_keys once (major speedup for event mode) ----
        existing_match_keys: set[str] = set()
        if event_key:
            async with app.state.db_pool.acquire() as conn:
                rows = await conn.fetch("SELECT match_key FROM match_results WHERE event_key=$1", event_key)
            existing_match_keys = {r["match_key"] for r in rows if r["match_key"] is not None}

        year_matches_played_applied = 0
        year_matches_skipped_unplayed = 0
        teams_in_played_matches: set[str] = set()

        # ---- Apply new matches & persist ----
        async with app.state.db_pool.acquire() as conn:
            async with conn.transaction():
                global LAST_EVENT_KEY, LAST_YEAR, processed_match_keys

                if event_key:
                    LAST_EVENT_KEY = event_key
                    LAST_YEAR = None
                else:
                    LAST_EVENT_KEY = None
                    LAST_YEAR = year_int

                    if fetched_event_keys:
                        await conn.execute(
                            "DELETE FROM team_history WHERE event_key = ANY($1::text[])",
                            fetched_event_keys,
                        )
                        await conn.execute(
                            "DELETE FROM match_results WHERE event_key = ANY($1::text[])",
                            fetched_event_keys,
                        )

                    await conn.execute("DELETE FROM team_current")

                    TEAM_RATINGS.clear()
                    sorted_year_teams = sorted(year_team_keys)
                    for team in sorted_year_teams:
                        TEAM_RATINGS[team] = env.create_rating()

                    if sorted_year_teams:
                        baseline_current_vals = [
                            (team, float(TEAM_RATINGS[team].mu), float(TEAM_RATINGS[team].sigma))
                            for team in sorted_year_teams
                        ]
                        await conn.executemany(
                            "INSERT INTO team_current(team,mu,sigma) VALUES($1,$2,$3)",
                            baseline_current_vals,
                        )

                for match in matches:
                    alliances = match.get("alliances")
                    if not alliances:
                        continue

                    red = alliances.get("red", {})
                    blue = alliances.get("blue", {})

                    teams1 = red.get("team_keys") or []
                    teams2 = blue.get("team_keys") or []
                    score1 = red.get("score")
                    score2 = blue.get("score")

                    # Skip unplayed matches
                    if score1 is None or score2 is None or score1 < 0 or score2 < 0:
                        if not event_key:
                            year_matches_skipped_unplayed += 1
                        continue

                    match_key = match.get("key")
                    if not match_key:
                        continue

                    teams1_norm = [str(t).strip().lower() for t in teams1 if str(t).strip()]
                    teams2_norm = [str(t).strip().lower() for t in teams2 if str(t).strip()]
                    if not teams1_norm or not teams2_norm:
                        continue

                    # Skip if already recorded (no per-match DB query now)
                    if event_key and match_key in existing_match_keys:
                        continue

                    # Decide ranks
                    if score1 > score2:
                        ranks = [0, 1]
                    elif score2 > score1:
                        ranks = [1, 0]
                    else:
                        ranks = [0, 0]

                    # Update ratings
                    ratings1 = [get_team_rating(t) for t in teams1_norm]
                    ratings2 = [get_team_rating(t) for t in teams2_norm]
                    new_r1, new_r2 = env.rate([ratings1, ratings2], ranks=ranks)

                    for t, new_r in zip(teams1_norm, new_r1):
                        TEAM_RATINGS[t] = new_r
                    for t, new_r in zip(teams2_norm, new_r2):
                        TEAM_RATINGS[t] = new_r

                    # Optional margin-of-victory extra updates (your logic preserved)
                    if score1 != score2:
                        if score1 > score2:
                            ratio = score1 / (score2 if score2 else 1)
                            winner, loser = teams1_norm, teams2_norm
                        else:
                            ratio = score2 / (score1 if score1 else 1)
                            winner, loser = teams2_norm, teams1_norm

                        extra = 0
                        if ratio >= 2.3:
                            extra = 1
                        if ratio >= 3.2:
                            extra = 2

                        w_ratings = [TEAM_RATINGS[t] for t in winner]
                        l_ratings = [TEAM_RATINGS[t] for t in loser]

                        for _ in range(extra):
                            w_new, l_new = env.rate([w_ratings, l_ratings], ranks=[0, 1])
                            for t, new_r in zip(winner, w_new):
                                TEAM_RATINGS[t] = new_r
                            for t, new_r in zip(loser, l_new):
                                TEAM_RATINGS[t] = new_r
                            w_ratings, l_ratings = w_new, l_new

                    match_time = match.get("actual_time") or match.get("time")
                    match_time = int(match_time) if match_time else None
                    evk = event_key or match.get("event_key")

                    # Persist match
                    await conn.execute(
                        "INSERT INTO match_results(match_key, event_key, red_score, blue_score, red_teams, blue_teams, time) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
                        match_key,
                        evk,
                        score1,
                        score2,
                        ",".join(teams1_norm),
                        ",".join(teams2_norm),
                        match_time,
                    )

                    # Persist history + current (kept as your per-match batch)
                    history_vals = []
                    current_vals = []
                    for t in teams1_norm + teams2_norm:
                        r = TEAM_RATINGS[t]
                        history_vals.append((t, float(r.mu), float(r.sigma), match_key, evk, match_time))
                        current_vals.append((t, float(r.mu), float(r.sigma)))

                    await conn.executemany(
                        "INSERT INTO team_history(team,mu,sigma,match_key,event_key,time) VALUES($1,$2,$3,$4,$5,$6)",
                        history_vals,
                    )
                    await conn.executemany(
                        "INSERT INTO team_current(team,mu,sigma) VALUES($1,$2,$3) "
                        "ON CONFLICT(team) DO UPDATE SET mu=excluded.mu, sigma=excluded.sigma",
                        current_vals,
                    )

                    processed_match_keys.add(match_key)
                    if event_key:
                        existing_match_keys.add(match_key)  # keep set in sync during this run
                    else:
                        year_matches_played_applied += 1
                        teams_in_played_matches.update(teams1_norm)
                        teams_in_played_matches.update(teams2_norm)

        if not event_key:
            teams_without_played_match = year_team_keys - teams_in_played_matches
            duration_ms = int((monotonic() - run_started_at) * 1000)
            logger.info(
                "year_update_coverage %s",
                json.dumps(
                    {
                        "year": year_int,
                        "events_discovered": len(fetched_event_keys),
                        "events_fetched": year_events_fetched,
                        "teams_discovered": len(year_team_keys),
                        "teams_seeded": len(year_team_keys),
                        "matches_received": len(matches),
                        "matches_played_applied": year_matches_played_applied,
                        "matches_skipped_unplayed": year_matches_skipped_unplayed,
                        "teams_with_no_played_match": len(teams_without_played_match),
                        "duration_ms": duration_ms,
                    },
                    sort_keys=True,
                ),
            )

        await broadcast_leaderboard()
        return {"status": "rankings updated", "teams_indexed": len(TEAM_RATINGS)}

@app.post("/push_results")
async def push_results(request: Request):
    """
    Apply additional match results (provided by client) to update ratings.
    Body: JSON list of {teams1, teams2, score1, score2}.
    Postgres-only implementation.
    """
    data = await request.json()
    if data is None:
        return JSONResponse({"error": "No JSON body provided"}, status_code=400)
    if not isinstance(data, list):
        return JSONResponse({"error": "Request body must be a JSON list"}, status_code=400)

    applied_count = 0

    async with update_lock:
        async with app.state.db_pool.acquire() as conn:
            async with conn.transaction():
                for match in data:
                    if not isinstance(match, dict):
                        continue

                    teams1 = match.get("teams1") or []
                    teams2 = match.get("teams2") or []
                    score1 = match.get("score1")
                    score2 = match.get("score2")

                    if not isinstance(teams1, list) or not isinstance(teams2, list):
                        continue
                    if score1 is None or score2 is None:
                        continue
                    if not teams1 or not teams2:
                        continue

                    teams1_norm = [str(t).strip().lower() for t in teams1 if str(t).strip()]
                    teams2_norm = [str(t).strip().lower() for t in teams2 if str(t).strip()]
                    if not teams1_norm or not teams2_norm:
                        continue

                    # Decide ranks
                    if score1 > score2:
                        ranks = [0, 1]
                    elif score2 > score1:
                        ranks = [1, 0]
                    else:
                        ranks = [0, 0]

                    ratings1 = [get_team_rating(t) for t in teams1_norm]
                    ratings2 = [get_team_rating(t) for t in teams2_norm]
                    new_r1, new_r2 = env.rate([ratings1, ratings2], ranks=ranks)

                    for t, new_r in zip(teams1_norm, new_r1):
                        TEAM_RATINGS[t] = new_r
                    for t, new_r in zip(teams2_norm, new_r2):
                        TEAM_RATINGS[t] = new_r

                    # Margin-of-victory extra updates (match /update behavior: 2.3, 3.2)
                    if score1 != score2:
                        if score1 > score2:
                            ratio = score1 / (score2 if score2 else 1)
                            winner, loser = teams1_norm, teams2_norm
                        else:
                            ratio = score2 / (score1 if score1 else 1)
                            winner, loser = teams2_norm, teams1_norm

                        extra = 0
                        if ratio >= 2.3:
                            extra = 1
                        if ratio >= 3.2:
                            extra = 2

                        w_ratings = [TEAM_RATINGS[t] for t in winner]
                        l_ratings = [TEAM_RATINGS[t] for t in loser]
                        for _ in range(extra):
                            w_new, l_new = env.rate([w_ratings, l_ratings], ranks=[0, 1])
                            for t, new_r in zip(winner, w_new):
                                TEAM_RATINGS[t] = new_r
                            for t, new_r in zip(loser, l_new):
                                TEAM_RATINGS[t] = new_r
                            w_ratings, l_ratings = w_new, l_new

                    # Persist as a synthetic match
                    match_time = int(datetime.now(timezone.utc).timestamp())
                    match_key = f"manual_{match_time}_{uuid.uuid4().hex[:10]}"

                    await conn.execute(
                        "INSERT INTO match_results(match_key, event_key, red_score, blue_score, red_teams, blue_teams, time) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(match_key) DO NOTHING",
                        match_key,
                        None,
                        int(score1),
                        int(score2),
                        ",".join(teams1_norm),
                        ",".join(teams2_norm),
                        match_time,
                    )

                    history_vals = []
                    current_vals = []
                    for t in teams1_norm + teams2_norm:
                        r = TEAM_RATINGS[t]
                        history_vals.append((t, float(r.mu), float(r.sigma), match_key, None, match_time))
                        current_vals.append((t, float(r.mu), float(r.sigma)))

                    await conn.executemany(
                        "INSERT INTO team_history(team,mu,sigma,match_key,event_key,time) VALUES($1,$2,$3,$4,$5,$6)",
                        history_vals,
                    )
                    await conn.executemany(
                        "INSERT INTO team_current(team,mu,sigma) VALUES($1,$2,$3) "
                        "ON CONFLICT(team) DO UPDATE SET mu=excluded.mu, sigma=excluded.sigma",
                        current_vals,
                    )

                    applied_count += 1

    await broadcast_leaderboard()
    return {"status": "results incorporated", "applied": applied_count}

@app.get("/predict_team")
async def predict_team(team: Optional[str] = None):
    """Get current TrueSkill rating for a team (with confidence and conservative rating)."""
    if not team:
        return JSONResponse({"error": "Missing team parameter"}, status_code=400)
    k = str(team).strip().lower()
    if k not in TEAM_RATINGS:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    rating = TEAM_RATINGS[k]
    mu_val = float(rating.mu)
    sigma_val = float(rating.sigma)
    confidence_percent = round(team_confidence_from_sigma(sigma_val), 2)
    return {
        "team": k,
        "mu": mu_val,
        "sigma": sigma_val,
        "conservative_mu_3sigma": mu_val - 3.0 * sigma_val,
        "confidence_percent": confidence_percent
    }

@app.post("/predict_match")
async def predict_match(request: Request):
    """Predict win probability for a matchup between two alliances."""
    data = await request.json()
    if data is None:
        return JSONResponse({"error": "No JSON body provided"}, status_code=400)
    teams1 = data.get("teams1") or []
    teams2 = data.get("teams2") or []
    if not teams1 or not teams2:
        return JSONResponse({"error": "teams1 and teams2 must be provided"}, status_code=400)
    ratings1 = [get_team_rating(t) for t in teams1]
    ratings2 = [get_team_rating(t) for t in teams2]
    mu1 = sum(r.mu for r in ratings1)
    mu2 = sum(r.mu for r in ratings2)
    sigma_sq_sum = sum((r.sigma ** 2) for r in (ratings1 + ratings2))
    N = len(ratings1) + len(ratings2)
    delta_mu = mu1 - mu2
    beta = env.beta
    denom = math.sqrt(N * (beta ** 2) + sigma_sq_sum)
    # Win probability for alliance1
    win_prob = float(env.cdf(delta_mu / denom)) if denom != 0 else 0.5
    prediction_conf = abs(2.0 * win_prob - 1.0) * 100.0  # how far from 50%
    return {
        "team1_win_prob": win_prob,
        "team2_win_prob": 1.0 - win_prob,
        "prediction_confidence_percent": round(prediction_conf, 2)
    }

@app.post("/predict_batch")
async def predict_batch(request: Request):
    """Predict win probabilities for multiple matchups in one request."""
    data = await request.json()
    if data is None:
        return JSONResponse({"error": "No JSON body provided"}, status_code=400)
    if not isinstance(data, list):
        return JSONResponse({"error": "Request body must be a JSON list"}, status_code=400)
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
    return results

@app.post("/recalculate")
async def recalculate_values(request: Request):
    """
    Recompute derived values for all teams and save to JSON file.
    Optional body: {"source": "json"} to reload from file first.
    """
    global env  # IMPORTANT: declare global before any use of env

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Yo Gurt! Why are you here? You should be in the fridge! But since you are here, let's have some fun with JSON parsing, shall we? Just kidding, let's get back to business.
    source = str(body.get("source", "memory")).lower()
    count_before = len(TEAM_RATINGS)

    if source == "json":
        try:
            path = os.environ.get("TRUESKILL_DATA_PATH", "DB/JSON/trueskill_data.json")
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            # If provided, reset env from JSON (use saved hyperparameters)
            meta_env = (payload.get("meta") or {}).get("env") or {}
            if meta_env:
                try:
                    mu = float(meta_env.get("mu", env.mu))
                    sigma = float(meta_env.get("sigma", env.sigma))
                    beta = float(meta_env.get("beta", env.beta))
                    tau = float(meta_env.get("tau", env.tau))
                    draw_prob = float(
                        meta_env.get("draw_probability", env.draw_probability)
                    )
                    env = trueskill.TrueSkill(
                        mu=mu,
                        sigma=sigma,
                        beta=beta,
                        tau=tau,
                        draw_probability=draw_prob,
                    )
                except Exception:
                    pass

            TEAM_RATINGS.clear()
            for entry in (payload.get("teams") or []):
                key = str(entry.get("team_key", "")).strip().lower()
                mu_val = entry.get("mu")
                sigma_val = entry.get("sigma")
                if key and mu_val is not None and sigma_val is not None:
                    TEAM_RATINGS[key] = env.create_rating(
                        mu=float(mu_val),
                        sigma=float(sigma_val),
                    )
            count_before = len(TEAM_RATINGS)
        except FileNotFoundError:
            return JSONResponse(
                {"error": "No data file found. Run /upload_data first."},
                status_code=404,
            )
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"error": f"Corrupt JSON: {e}"},
                status_code=500,
            )
        except Exception as e:
            return JSONResponse(
                {"error": f"Recalculate failed: {e}"},
                status_code=500,
            )

    # Build payload and save to JSON file
    payload = {
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
        "teams": [
            serialize_team_entry(team, rating)
            for team, rating in sorted(TEAM_RATINGS.items())
        ],
    }

    try:
        path = os.environ.get("TRUESKILL_DATA_PATH", "trueskill_data.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to write JSON: {e}"},
            status_code=500,
        )

    return {
        "status": "recalculated",
        "source": source,
        "teams_indexed": count_before,
        "file": os.environ.get("TRUESKILL_DATA_PATH", "trueskill_data.json"),
        "saved_teams_indexed": len(TEAM_RATINGS),
        "env": payload["meta"]["env"],
        "context": payload["meta"]["context"],
    }

@app.post("/upload_data")
async def upload_data():
    """Persist current team data to trueskill_data.json."""
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "The Blue Alliance (processed locally)",
            "env": {
                "mu": float(env.mu),
                "sigma": float(env.sigma),
                "beta": float(env.beta),
                "tau": float(env.tau),
                "draw_probability": float(env.draw_probability)
            },
            "context": {
                "event_key": LAST_EVENT_KEY,
                "year": LAST_YEAR,
                "teams_indexed": len(TEAM_RATINGS)
            }
        },
        "teams": [serialize_team_entry(team, rating) for team, rating in TEAM_RATINGS.items()]
    }
    try:
        path = os.environ.get("TRUESKILL_DATA_PATH", "trueskill_data.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return {"status": "saved", "file": path, "teams_indexed": len(TEAM_RATINGS)}
    except Exception as e:
        return JSONResponse({"error": f"Failed to write data: {e}"}, status_code=500)

@app.post("/load_data")
async def load_data_from_json(request: Request):
    """
        Load ratings from a JSON file into memory (does not modify database).
        Body (optional): { "path": "...", "use_env_from_json": true }
    """
    global env  # IMPORTANT: declare global before any use of env

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    path = body.get("path") or os.environ.get(
        "TRUESKILL_DATA_PATH", "trueskill_data.json"
    )
    use_env_from_json = bool(body.get("use_env_from_json", True))

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"No data file found at {path}. Run /upload_data first."},
            status_code=404,
        )
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"error": f"Corrupt JSON in {path}: {e}"},
            status_code=500,
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to load data from {path}: {e}"},
            status_code=500,
        )

    if use_env_from_json:
        meta_env = (payload.get("meta") or {}).get("env") or {}
        try:
            mu = float(meta_env.get("mu", env.mu))
            sigma = float(meta_env.get("sigma", env.sigma))
            beta = float(meta_env.get("beta", env.beta))
            tau = float(meta_env.get("tau", env.tau))
            draw_prob = float(
                meta_env.get("draw_probability", env.draw_probability)
            )
            env = trueskill.TrueSkill(
                mu=mu,
                sigma=sigma,
                beta=beta,
                tau=tau,
                draw_probability=draw_prob,
            )
        except Exception:
            pass

    TEAM_RATINGS.clear()
    for entry in (payload.get("teams") or []):
        key = str(entry.get("team_key", "")).strip().lower()
        mu_val = entry.get("mu")
        sigma_val = entry.get("sigma")
        if key and mu_val is not None and sigma_val is not None:
            TEAM_RATINGS[key] = env.create_rating(
                mu=float(mu_val),
                sigma=float(sigma_val),
            )

    return {
        "status": "loaded",
        "file": path,
        "use_env_from_json": use_env_from_json,
        "teams_indexed": len(TEAM_RATINGS),
        "context": {"event_key": LAST_EVENT_KEY, "year": LAST_YEAR},
    }


@app.get("/leaderboard")
async def get_leaderboard():
    """
        Get all teams' ratings, sorted by conservative rating (descending).
    """
    if not TEAM_RATINGS:
        return {"teams": [], "teams_indexed": 0}
    return build_leaderboard_data()

@app.get("/team_history/{team_id}")
async def team_history(team_id: str):
    """
    Get full rating history for a team (list of mu, sigma over time).
    """
    team_key = str(team_id).strip().lower()
    async with app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT mu, sigma, time, match_key, event_key "
            "FROM team_history WHERE team=$1 "
            "ORDER BY time ASC NULLS LAST, id ASC",
            team_key,
        )

    history = []
    for r in rows:
        mu_val = float(r["mu"])
        sigma_val = float(r["sigma"])
        history.append(
            {
                "mu": mu_val,
                "sigma": sigma_val,
                "conservative": mu_val - 3.0 * sigma_val,
                "time": r["time"],
                "match_key": r["match_key"],
                "event_key": r["event_key"],
            }
        )

    return {"team": team_key, "history": history}

@app.get("/compare")
async def compare_teams(team1: Optional[str] = None, team2: Optional[str] = None):
    """
    Compare two teams: returns each team's rating, win probability for team1 vs team2, and both histories.
    """
    if not team1 or not team2:
        return JSONResponse({"error": "Must provide team1 and team2"}, status_code=400)

    t1 = str(team1).strip().lower()
    t2 = str(team2).strip().lower()

    if t1 not in TEAM_RATINGS or t2 not in TEAM_RATINGS:
        return JSONResponse({"error": "One or both teams not found"}, status_code=404)

    r1 = TEAM_RATINGS[t1]
    r2 = TEAM_RATINGS[t2]

    team1_data = serialize_team_entry(t1, r1)
    team2_data = serialize_team_entry(t2, r2)

    # Win probability (1v1)
    delta_mu = float(r1.mu - r2.mu)
    beta = float(env.beta)
    denom = math.sqrt(2.0 * (beta ** 2) + float(r1.sigma ** 2) + float(r2.sigma ** 2))
    win_prob = float(env.cdf(delta_mu / denom)) if denom != 0 else 0.5

    async with app.state.db_pool.acquire() as conn:
        hist1_rows = await conn.fetch(
            "SELECT mu, sigma, time FROM team_history WHERE team=$1 ORDER BY time ASC NULLS LAST, id ASC",
            t1,
        )
        hist2_rows = await conn.fetch(
            "SELECT mu, sigma, time FROM team_history WHERE team=$1 ORDER BY time ASC NULLS LAST, id ASC",
            t2,
        )

    history1 = [{"mu": float(r["mu"]), "sigma": float(r["sigma"]), "time": r["time"]} for r in hist1_rows]
    history2 = [{"mu": float(r["mu"]), "sigma": float(r["sigma"]), "time": r["time"]} for r in hist2_rows]

    return {
        "team1": team1_data,
        "team2": team2_data,
        "team1_win_prob": win_prob,
        "team2_win_prob": 1.0 - win_prob,
        "history1": history1,
        "history2": history2,
    }


@app.post("/picklist")
async def picklist(request: Request):
    """
        Generate and rank candidate 3-team alliances around a target team.
        Body (JSON): {
            "target_team": str,           # team key around which to form alliances
            "taken": [str],               # list of team keys already taken (excluded)
            "playoff_alliances": [[str]]  # (optional) list of opposing alliances (each a list of team keys)
        }
        Returns a JSON list of alliances with metrics:
            - "alliance": list of 3 team keys (including target_team)
            - "win_prob_avg": average win probability of this alliance vs provided opponents (if any)
            - "confidence_percent": average TrueSkill confidence (%) of teams in the alliance
        The list is sorted by descending win probability (if opponents provided) or by confidence.
    """

    data = await request.json()
    if data is None:
        return JSONResponse({"error": "No JSON body provided"}, status_code=400)

    # Validate target_team
    target_team = data.get("target_team")
    if not target_team or not isinstance(target_team, str):
        return JSONResponse({"error": "Missing or invalid 'target_team'"}, status_code=400)
    target_key = target_team.strip().lower()
    if target_key == "":
        return JSONResponse({"error": "Empty 'target_team'"}, status_code=400)

    # Validate taken list
    taken = data.get("taken", [])
    if not isinstance(taken, list):
        return JSONResponse({"error": "'taken' must be a list of team keys"}, status_code=400)
    # Normalize taken list to lower-case keys
    taken_keys = []
    for t in taken:
        if not isinstance(t, str):
            return JSONResponse({"error": "All entries in 'taken' must be team key strings"}, status_code=400)
        key = t.strip().lower()
        if key:
            taken_keys.append(key)
    # Check target not in taken
    if target_key in taken_keys:
        return JSONResponse({"error": "target_team cannot be in taken list"}, status_code=400)

    # Validate playoff_alliances if provided
    playoff_alliances = data.get("playoff_alliances", [])
    if playoff_alliances is None:
        playoff_alliances = []
    if not isinstance(playoff_alliances, list):
        return JSONResponse({"error": "'playoff_alliances' must be a list of alliances"}, status_code=400)
    opponents = []
    for alliance in playoff_alliances:
        if not isinstance(alliance, list):
            return JSONResponse({"error": "Each item in 'playoff_alliances' must be a list of team keys"}, status_code=400)
        opp_alliance = []
        for t in alliance:
            if not isinstance(t, str):
                return JSONResponse({"error": "Alliance entries must be team key strings"}, status_code=400)
            key = t.strip().lower()
            if key:
                opp_alliance.append(key)
        if opp_alliance:
            opponents.append(opp_alliance)
    # Check opponents teams exist in ratings
    for opp in opponents:
        for t in opp:
            if t not in TEAM_RATINGS:
                return JSONResponse({"error": f"Opponent team '{t}' not found in ratings"}, status_code=404)

    # Check that target_team exists in our ratings
    if target_key not in TEAM_RATINGS:
        return JSONResponse({"error": f"Target team '{target_team}' not found in ratings"}, status_code=404)

    # Build list of available teams (excluding target and taken)
    available = [team for team in TEAM_RATINGS.keys() if team != target_key and team not in taken_keys]
    if len(available) < 2:
        # Not enough teams to form a 3-team alliance
        return []

    # Sort available for consistent ordering of combinations
    available.sort()
    from itertools import combinations
    alliance_options = []
    for combo in combinations(available, 2):
        alliance_options.append((target_key, combo[0], combo[1]))

    results = []
    for team1, team2, team3 in alliance_options:
        alliance_key = [team1, team2, team3]
        win_probs = []
        # Simulate matches against each provided opponent alliance
        for opponent in opponents:
            # Compute win probability for [team1, team2, team3] vs opponent alliance
            ratings_team = [get_team_rating(t) for t in alliance_key]
            ratings_opp = [get_team_rating(t) for t in opponent]
            mu_team = sum(r.mu for r in ratings_team)
            mu_opp = sum(r.mu for r in ratings_opp)
            sigma_sq = sum(r.sigma**2 for r in (ratings_team + ratings_opp))
            N = len(ratings_team) + len(ratings_opp)
            delta_mu = mu_team - mu_opp
            denom = math.sqrt(N * (env.beta ** 2) + sigma_sq)
            win_prob = float(env.cdf(delta_mu / denom)) if denom != 0 else 0.5
            win_probs.append(win_prob)
        # Compute average win probability if opponents provided
        avg_win = None
        if win_probs:
            avg_win = sum(win_probs) / len(win_probs)
        # Compute alliance confidence as average of team confidences
        confidences = []
        for t in alliance_key:
            rating = TEAM_RATINGS.get(t)
            if rating:
                confidences.append(team_confidence_from_sigma(rating.sigma))
            else:
                confidences.append(0.0)
        if not confidences:
            continue
        avg_conf = round(sum(confidences) / len(confidences), 2)
        alliance_data = {
            "alliance": alliance_key,
            "confidence_percent": avg_conf
        }
        if avg_win is not None:
            alliance_data["win_prob_avg"] = avg_win
        results.append(alliance_data)

    # Sort results by descending win probability if available, else by confidence
    if opponents:
        results.sort(key=lambda x: x.get("win_prob_avg", 0), reverse=True)
    else:
        results.sort(key=lambda x: x.get("confidence_percent", 0), reverse=True)

    return results

@app.post("/teams/compare")
async def teams_compare(request: Request):
    """
        Compare multiple teams and their ratings.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid or missing JSON body"}, status_code=400)
    team_keys = data.get("teams", [])
    if not isinstance(team_keys, list):
        return JSONResponse({"error": "'teams' must be a list"}, status_code=400)
    
    results = []
    async with app.state.db_pool.acquire() as conn:
        for team in team_keys:
            team_norm = str(team).strip().lower()
            rating = TEAM_RATINGS.get(team_norm)
            if rating:
                entry = serialize_team_entry(team_norm, rating)
                rows = await conn.fetch(
                    "SELECT mu, sigma, match_key, event_key, time FROM team_history WHERE team=$1 ORDER BY time ASC",
                    team_norm,
                )
                history = [
                    {"mu": float(r["mu"]), "sigma": float(r["sigma"]),
                     "match_key": r["match_key"], "event_key": r["event_key"], "time": r["time"]}
                    for r in rows
                ]
                entry["history"] = history
            else:
                entry = {"team_key": team_norm, "error": "Team not found"}
            results.append(entry)
    return {"teams": results}

@app.get("/match/{match_key}/analysis")
async def match_analysis(match_key: str):
    """
        Analyze a specific match and provide predicted win probabilities based on current ratings. This does not modify ratings & the postgres DB.
    """
    async with app.state.db_pool.acquire() as conn:
        record = await conn.fetchrow("SELECT red_teams, blue_teams FROM match_results WHERE match_key=$1", match_key)
    if not record:
        return JSONResponse({"error": "Match not found"}, status_code=404)
    teams1 = record["red_teams"].split(",") if record["red_teams"] else []
    teams2 = record["blue_teams"].split(",") if record["blue_teams"] else []
    ratings1 = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams1]
    ratings2 = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams2]
    # Calculate win probability (TrueSkill)
    delta_mu = sum(r.mu for r in ratings1) - sum(r.mu for r in ratings2)
    sum_sigma_sq = sum(r.sigma**2 for r in ratings1 + ratings2)
    size = len(ratings1) + len(ratings2)
    denom = math.sqrt(size * (env.beta**2) + sum_sigma_sq)
    prob_red = env.cdf(delta_mu / denom)
    return {"match_key": match_key, "teams_red": teams1, "teams_blue": teams2,
            "predicted_red_win_prob": prob_red, "predicted_blue_win_prob": 1 - prob_red}

@app.get("/event/{event_key}/upsets")
async def event_upsets(event_key: str):
    """
        Analyze all matches in an event and identify upsets (where predicted winner lost). This uses current ratings & the postgres DB.
    """
    async with app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT match_key, red_teams, blue_teams, red_score, blue_score FROM match_results WHERE event_key=$1", event_key
        )
    upsets = []
    for r in rows:
        teams1 = r["red_teams"].split(",") if r["red_teams"] else []
        teams2 = r["blue_teams"].split(",") if r["blue_teams"] else []
        ratings1 = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams1]
        ratings2 = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams2]
        delta_mu = sum(r.mu for r in ratings1) - sum(r.mu for r in ratings2)
        sum_sigma_sq = sum(r.sigma**2 for r in ratings1 + ratings2)
        size = len(ratings1) + len(ratings2)
        denom = math.sqrt(size * (env.beta**2) + sum_sigma_sq)
        prob_red = env.cdf(delta_mu / denom)
        predicted_red = prob_red >= 0.5
        actual_red = r["red_score"] > r["blue_score"]
        if predicted_red != actual_red:
            upsets.append({
                "match_key": r["match_key"],
                "teams_red": teams1, "teams_blue": teams2,
                "score_red": r["red_score"], "score_blue": r["blue_score"],
                "predicted_red_win_prob": prob_red
            })
    return {"event_key": event_key, "upsets": upsets}

@app.post("/predict_alliance")
async def predict_alliance(request: Request):
    """
        Predict win probability for a 3v3 alliance matchup. This is the same as /predict_match but enforces 3 teams per side and returns less data.
        Uses the current ratings & the postgres DB.
    """
    data = await request.json()
    teams_red = data.get("teams_red", [])
    teams_blue = data.get("teams_blue", [])

    if not teams_red or not teams_blue or len(teams_red) != 3 or len(teams_blue) != 3:
        return JSONResponse({"error": "Provide two lists of 3 team keys each"}, status_code=400)
    
    ratings_red = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams_red]
    ratings_blue = [TEAM_RATINGS.get(t.lower()) or trueskill.Rating() for t in teams_blue]
    delta_mu = float(sum(r.mu for r in ratings_red)) - float(sum(r.mu for r in ratings_blue))
    sum_sigma_sq = float(sum(r.sigma**2 for r in ratings_red + ratings_blue))
    size = len(ratings_red) + len(ratings_blue)
    denom = math.sqrt(size * (env.beta**2) + sum_sigma_sq)
    prob_red = env.cdf(delta_mu / denom)
    return {"red_win_probability": prob_red, "blue_win_probability": 1 - prob_red}

# -------------------------------------------------------------------------------------------------------- #

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for live updates."""
    await websocket.accept()
    clients.add(websocket)
    try:
        # Keep connection open, waiting for client disconnect
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)

# -------------------------------------------------------------------------------------------------------- #

class _AutomationRequest:
    """Minimal request-like object so automation can reuse /update logic."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


def _resolve_season_year(now_local: Optional[datetime] = None) -> int:
    now_local = now_local or datetime.now().astimezone()
    raw = (os.environ.get("TRUESKILL_SEASON_YEAR") or "").strip()
    if raw:
        try:
            return int(raw)
        except Exception:
            logger.warning("Invalid TRUESKILL_SEASON_YEAR=%r, defaulting to current local year.", raw)
    return int(now_local.year)


def _next_nightly_full_rebuild_time(now_local: datetime) -> datetime:
    candidate = now_local.replace(
        hour=3,
        minute=random.randint(0, 59),
        second=random.randint(0, 59),
        microsecond=0,
    )
    if candidate <= now_local:
        tomorrow = now_local + timedelta(days=1)
        candidate = tomorrow.replace(
            hour=3,
            minute=random.randint(0, 59),
            second=random.randint(0, 59),
            microsecond=0,
        )
    return candidate


def _json_response_error(resp: JSONResponse) -> str:
    try:
        raw_body = resp.body
        if raw_body is None:
            body_bytes = b"{}"
        elif isinstance(raw_body, memoryview):
            body_bytes = raw_body.tobytes()
        elif isinstance(raw_body, (bytes, bytearray)):
            body_bytes = bytes(raw_body)
        else:
            body_bytes = str(raw_body).encode("utf-8")
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        msg = body.get("error") or body.get("detail") or body.get("message")
        if isinstance(msg, str) and msg:
            return msg
    return f"HTTP {resp.status_code}"


async def _run_automated_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Run /update internally while preserving manual context state."""
    global LAST_EVENT_KEY, LAST_YEAR
    prev_event = LAST_EVENT_KEY
    prev_year = LAST_YEAR
    try:
        result = await update_ratings(cast(Request, _AutomationRequest(payload)))
    except Exception as exc:
        return {"ok": False, "status": "exception", "error": str(exc)}
    finally:
        LAST_EVENT_KEY = prev_event
        LAST_YEAR = prev_year

    if isinstance(result, JSONResponse):
        return {
            "ok": False,
            "status": "error",
            "error": _json_response_error(result),
            "status_code": result.status_code,
        }

    if isinstance(result, dict):
        return {
            "ok": True,
            "status": str(result.get("status") or "unknown"),
            "teams_indexed": result.get("teams_indexed"),
            "event_key": result.get("event_key"),
        }

    return {"ok": False, "status": "invalid_result", "error": "Unexpected /update return payload"}


def _event_is_active(event_payload: dict[str, Any], today_iso: str) -> bool:
    start_date = str(event_payload.get("start_date") or "").strip()
    end_date = str(event_payload.get("end_date") or "").strip()
    if start_date and today_iso < start_date:
        return False
    if end_date and today_iso > end_date:
        return False
    return bool(start_date or end_date)


async def _fetch_active_event_keys_for_year(
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
    year: int,
    today_iso: str,
) -> tuple[list[str], bool]:
    resource = f"events/{year}/simple"
    etag: Optional[str] = None

    async with app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT etag FROM etag_cache WHERE resource=$1", resource)
        if row and row.get("etag"):
            etag = str(row["etag"])

    headers = dict(base_headers)
    if etag:
        headers["If-None-Match"] = etag

    url = f"https://www.thebluealliance.com/api/v3/events/{year}/simple"
    resp = await _tba_get_with_retry(client, url, headers)

    if resp.status_code == 304:
        cache = ACTIVE_EVENTS_CACHE.get(year)
        if cache and cache.get("date") == today_iso and isinstance(cache.get("event_keys"), list):
            return [str(k) for k in cache["event_keys"]], True
        # No usable cache for today; fetch payload once without conditional header.
        resp = await _tba_get_with_retry(client, url, base_headers)

    if resp.status_code in (401, 403):
        detail = (resp.text or "")[:300]
        raise RuntimeError(f"TBA auth failed ({resp.status_code}) while fetching events/{year}/simple: {detail}")

    if resp.status_code != 200:
        detail = (resp.text or "")[:300]
        raise RuntimeError(f"TBA API error {resp.status_code} while fetching events/{year}/simple: {detail}")

    payload = resp.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"TBA payload malformed for events/{year}/simple")

    active_keys: set[str] = set()
    for ev in payload:
        if not isinstance(ev, dict):
            continue
        if not _event_is_active(ev, today_iso):
            continue
        key = str(ev.get("key") or "").strip()
        if key:
            active_keys.add(key)

    active_list = sorted(active_keys)
    ACTIVE_EVENTS_CACHE[year] = {"date": today_iso, "event_keys": active_list}

    new_etag = resp.headers.get("ETag")
    if new_etag:
        async with app.state.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO etag_cache(resource, etag) VALUES($1, $2) "
                "ON CONFLICT(resource) DO UPDATE SET etag=excluded.etag",
                resource,
                new_etag,
            )

    return active_list, False


async def run_active_events_cycle() -> dict[str, Any]:
    cycle_started = monotonic()
    tba_key = (os.environ.get("TBA_AUTH_KEY") or os.environ.get("VITE_TBA_API_KEY") or "").strip()
    if not tba_key:
        return {
            "status": "skipped",
            "reason": "missing_tba_key",
            "duration_ms": int((monotonic() - cycle_started) * 1000),
        }

    year = _resolve_season_year()
    today_iso = datetime.now().astimezone().date().isoformat()
    user_agent = os.environ.get("TBA_USER_AGENT", "Team3173-TrueSkillAPI/4.0.4")
    base_headers = {
        "X-TBA-Auth-Key": tba_key,
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }

    async with httpx.AsyncClient(
        timeout=20.0,
        http2=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=10, keepalive_expiry=30.0),
    ) as client:
        active_keys, used_cache = await _fetch_active_event_keys_for_year(client, base_headers, year, today_iso)

    if not active_keys:
        return {
            "status": "skipped",
            "reason": "no_active_events",
            "year": year,
            "active_events": 0,
            "event_list_cache_hit": used_cache,
            "duration_ms": int((monotonic() - cycle_started) * 1000),
        }

    updated_events = 0
    no_change_events = 0
    failed_events = 0

    for event_key in active_keys:
        result = await _run_automated_update({"event_key": event_key})
        if not result.get("ok"):
            failed_events += 1
            logger.warning("Active-event update failed for %s: %s", event_key, result.get("error"))
            continue

        if result.get("status") == "no new data":
            no_change_events += 1
        else:
            updated_events += 1

    return {
        "status": "completed",
        "year": year,
        "active_events": len(active_keys),
        "updated_events": updated_events,
        "no_change_events": no_change_events,
        "failed_events": failed_events,
        "event_list_cache_hit": used_cache,
        "duration_ms": int((monotonic() - cycle_started) * 1000),
    }


async def active_events_loop():
    logger.info("Active-event automation enabled: every 300 seconds.")
    while True:
        try:
            summary = await run_active_events_cycle()
            logger.info("active_events_cycle %s", json.dumps(summary, sort_keys=True))
        except Exception:
            logger.exception("Active-event cycle crashed")
        await asyncio.sleep(300)


async def nightly_full_rebuild_loop():
    logger.info("Nightly full rebuild automation enabled: random local time between 03:00 and 03:59 daily.")
    while True:
        now_local = datetime.now().astimezone()
        run_at = _next_nightly_full_rebuild_time(now_local)
        delay_seconds = max(0.0, (run_at - now_local).total_seconds())
        logger.info("Next nightly full rebuild scheduled at %s", run_at.isoformat())
        await asyncio.sleep(delay_seconds)

        run_started = monotonic()
        year = _resolve_season_year()
        result = await _run_automated_update({"year": year})
        duration_ms = int((monotonic() - run_started) * 1000)
        if result.get("ok"):
            logger.info(
                "nightly_full_rebuild %s",
                json.dumps(
                    {
                        "status": result.get("status"),
                        "year": year,
                        "teams_indexed": result.get("teams_indexed"),
                        "duration_ms": duration_ms,
                    },
                    sort_keys=True,
                ),
            )
        else:
            logger.error(
                "nightly_full_rebuild_failed %s",
                json.dumps(
                    {"year": year, "error": result.get("error"), "duration_ms": duration_ms},
                    sort_keys=True,
                ),
            )

# Startup event: Initialize PostgreSQL pool and tables
async def init_postgres():
    """
    Initialize PostgreSQL connection pool and required tables.
    Loads TEAM_RATINGS from team_current (Postgres is the only source of truth).
    """
    dsn = (os.environ.get("TRUESKILL_DB_URI") or "").strip()
    if not dsn:
        raise RuntimeError(
            "TRUESKILL_DB_URI is required (example: postgresql://user:pass@localhost:5432/trueskill)"
        )

    def _is_missing_database_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            isinstance(exc, asyncpg.InvalidCatalogNameError)
            or ("database" in msg and "does not exist" in msg)
        )

    def _pg_quote_ident(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _extract_database_name(raw_dsn: str) -> str:
        parsed = urlsplit(raw_dsn)
        name = unquote((parsed.path or "").lstrip("/"))
        if not name:
            raise RuntimeError("TRUESKILL_DB_URI must include a database name (path segment).")
        return name

    def _dsn_with_database(raw_dsn: str, database_name: str) -> str:
        parsed = urlsplit(raw_dsn)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/" + quote(database_name, safe=""),
                parsed.query,
                parsed.fragment,
            )
        )

    async def _ensure_database_exists(raw_dsn: str) -> None:
        target_db = _extract_database_name(raw_dsn)
        admin_uri = (os.environ.get("TRUESKILL_DB_ADMIN_URI") or "").strip()
        if admin_uri:
            candidate_admin_dsns = [admin_uri]
        else:
            admin_db = (os.environ.get("TRUESKILL_DB_ADMIN_DB") or "postgres").strip() or "postgres"
            candidate_admin_dsns = [_dsn_with_database(raw_dsn, admin_db)]
            if admin_db.lower() != "template1":
                candidate_admin_dsns.append(_dsn_with_database(raw_dsn, "template1"))

        attempts: list[str] = []
        for admin_dsn in candidate_admin_dsns:
            conn = None
            try:
                conn = await asyncpg.connect(dsn=admin_dsn, command_timeout=30)
                exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", target_db)
                if exists:
                    return
                await conn.execute(f"CREATE DATABASE {_pg_quote_ident(target_db)}")
                return
            except Exception as exc:
                attempts.append(f"{admin_dsn} -> {exc}")
            finally:
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()

        details = " | ".join(attempts) if attempts else "no admin connection attempts were possible"
        raise RuntimeError(
            "Database auto-setup failed. "
            "Set TRUESKILL_DB_URI to an existing database, or set TRUESKILL_DB_ADMIN_URI "
            "to a superuser/admin connection so the API can create the DB automatically. "
            f"Details: {details}"
        )

    async def _open_pool(raw_dsn: str):
        pool = await asyncpg.create_pool(
            dsn=raw_dsn,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return pool

    try:
        app.state.db_pool = await _open_pool(dsn)
    except Exception as e:
        if not _is_missing_database_error(e):
            raise RuntimeError(f"Database connection failed: {e}")
        try:
            await _ensure_database_exists(dsn)
            app.state.db_pool = await _open_pool(dsn)
        except Exception as inner:
            raise RuntimeError(f"Database connection failed after auto-setup attempt: {inner}")

    async with app.state.db_pool.acquire() as conn:
        # Core tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_results (
                id SERIAL PRIMARY KEY,
                match_key TEXT UNIQUE,
                event_key TEXT,
                red_score INTEGER,
                blue_score INTEGER,
                red_teams TEXT,
                blue_teams TEXT,
                time INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS team_history (
                id SERIAL PRIMARY KEY,
                team TEXT,
                mu DOUBLE PRECISION,
                sigma DOUBLE PRECISION,
                match_key TEXT,
                event_key TEXT,
                time INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS team_current (
                team TEXT PRIMARY KEY,
                mu DOUBLE PRECISION,
                sigma DOUBLE PRECISION
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS etag_cache (
                resource TEXT PRIMARY KEY,
                etag TEXT
            )
        """)

        # Helpful indexes
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_match_results_event ON match_results(event_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_team_history_team ON team_history(team)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_team_history_event ON team_history(event_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_match_results_time ON match_results(time)")

        rows = await conn.fetch("SELECT team, mu, sigma FROM team_current")

    TEAM_RATINGS.clear()
    for r in rows:
        team_key = str(r["team"]).strip().lower()
        TEAM_RATINGS[team_key] = env.create_rating(mu=float(r["mu"]), sigma=float(r["sigma"]))

async def startup():
    # Postgres is the only DB used
    await init_postgres()

    # Start scheduled automation loops
    app.state.active_events_task = asyncio.create_task(active_events_loop())
    app.state.nightly_full_task = asyncio.create_task(nightly_full_rebuild_loop())


async def shutdown():
    active_events_task = getattr(app.state, "active_events_task", None)
    if active_events_task:
        active_events_task.cancel()
        with suppress(asyncio.CancelledError):
            await active_events_task

    nightly_full_task = getattr(app.state, "nightly_full_task", None)
    if nightly_full_task:
        nightly_full_task.cancel()
        with suppress(asyncio.CancelledError):
            await nightly_full_task

    db_pool = getattr(app.state, "db_pool", None)
    if db_pool:
        await db_pool.close()

if hasattr(app, "add_event_handler"):
    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)
else:
    app.router.on_startup.append(startup)
    app.router.on_shutdown.append(shutdown)

if __name__ == "__main__":
    import uvicorn
    # Run app with Uvicorn (single worker to maintain shared state)
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=1)
