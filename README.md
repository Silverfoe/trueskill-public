# FIRST Team 3173's TrueSkill API

[![Developer](https://img.shields.io/badge/Developer-Jacob%20W-purple?style=flat)](https://jtech.dev)
[![Team 3173](https://img.shields.io/badge/Team%203173-igknighters.org-gold?style=flat)](https://igknighters.org)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white&style=flat)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-009688?logo=fastapi&logoColor=white&style=flat)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Database-4169E1?logo=postgresql&logoColor=white&style=flat)](https://www.postgresql.org/)

High-performance TrueSkill backend for **FIRST Robotics Competition (FRC)** match ingestion, team ratings, live predictions, historical analysis, alliance evaluation, and leaderboard broadcasting.

This README is updated to match the current implementation in `trueskill_api_v4.py`.

---

## Table of Contents

1. [Overview](#overview)
2. [What This Service Does](#what-this-service-does)
3. [Current Version](#current-version)
4. [Architecture Summary](#architecture-summary)
5. [Requirements](#requirements)
6. [Installation](#installation)
7. [Configuration](#configuration)
8. [Running the API](#running-the-api)
9. [TrueSkill Model Settings](#trueskill-model-settings)
10. [Data Flow](#data-flow)
11. [Persistence and Database Schema](#persistence-and-database-schema)
12. [Automation Loops](#automation-loops)
13. [API Reference](#api-reference)
14. [WebSocket Live Updates](#websocket-live-updates)
15. [JSON Snapshot Format](#json-snapshot-format)
16. [Operational Notes and Caveats](#operational-notes-and-caveats)
17. [Example Workflow](#example-workflow)
18. [Troubleshooting](#troubleshooting)
19. [License Notice](#license-notice)

---

## Overview

This API is a FastAPI-based TrueSkill service designed for FRC analytics. It consumes match data from **The Blue Alliance (TBA)**, calculates team ratings, stores match/history/current rating data in **PostgreSQL**, and exposes endpoints for:

- team ratings
- matchup prediction
- batch prediction
- leaderboard retrieval
- team-to-team comparison
- team history lookup
- alliance picklist generation
- upset analysis
- JSON snapshot export/import
- live leaderboard broadcasting over WebSockets (WIP)

It keeps an in-memory rating map for fast access and uses PostgreSQL as the persistent source of truth for match and history data.

---

## What This Service Does

At a high level, the service:

1. Pulls match results from TBA by **event** or by **entire season year**.
2. Rebuilds or updates in-memory TrueSkill ratings.
3. Writes match records and team history to PostgreSQL.
4. Maintains a current team snapshot in `team_current`.
5. Broadcasts updated leaderboard data to connected WebSocket clients.
6. Supports manual result injection for local testing or custom workflows.
7. Can save and load JSON snapshots of current ratings.

---

## Current Version

The FastAPI app is currently declared as:

- **Title:** `FIRST Team 3173's TrueSkill API`
- **Version:** `4.0.4`

---

## Architecture Summary

### Main file

- `trueskill_api_v4.py`

### Core runtime pieces

- **FastAPI** application for HTTP + WebSocket endpoints
- **TrueSkill environment** tuned for FRC
- **In-memory ratings map**
  - `TEAM_RATINGS: Dict[str, trueskill.Rating]`
- **Async lock**
  - `update_lock`
  - prevents concurrent mutation of ratings/database state
- **PostgreSQL connection pool**
  - stored at `app.state.db_pool`
- **WebSocket client registry**
  - `clients: set[WebSocket]`
- **Context tracking**
  - `LAST_EVENT_KEY`
  - `LAST_YEAR`
- **Processed match tracking**
  - `processed_match_keys`
- **Active event cache**
  - `ACTIVE_EVENTS_CACHE`

### External dependencies

- The Blue Alliance API
- PostgreSQL

### Important runtime behavior

- The service is designed to run with **one worker**.
- Using multiple Uvicorn workers would create multiple in-memory rating states.
    - This is condusive to chaos and the API not working.

---

## Requirements

### Python

- **Python 3.10+** recommended but **required for stability**

### Python packages

```bash
pip install fastapi uvicorn trueskill httpx asyncpg
```

Optional testing packages:

```bash
pip install pytest pytest-asyncio
```

### Infrastructure

- PostgreSQL database
- TBA API key for any endpoint that fetches live TBA data

---

## Installation

```bash
git clone https://github.com/your-org/trueskill-3173.git
cd trueskill-3173

python3 -m venv venv
source venv/bin/activate

pip install fastapi uvicorn trueskill httpx asyncpg
```

---

## Configuration

The API relies on environment variables.

### Required

#### `TRUESKILL_DB_URI`

PostgreSQL connection string.

Example:

```bash
export TRUESKILL_DB_URI="postgresql://USER:PASSWORD@HOST:5432/DBNAME"
```

#### `TBA_AUTH_KEY`

Preferred TBA API key.

```bash
export TBA_AUTH_KEY="YOUR_TBA_API_KEY"
```

### Accepted fallback key name

If `TBA_AUTH_KEY` is not set, the code also accepts:

```bash
export VITE_TBA_API_KEY="YOUR_TBA_API_KEY"
```

### Optional

#### `TBA_USER_AGENT`

Custom User-Agent sent to TBA.

Default:

```text
Team3173-TrueSkillAPI/4.0.4
```

Example:

```bash
export TBA_USER_AGENT="MyTeam3173App/1.0"
```

#### `TRUESKILL_DATA_PATH`

Path used by JSON save/load endpoints.

Example:

```bash
export TRUESKILL_DATA_PATH="trueskill_data.json"
```

#### `TRUESKILL_SEASON_YEAR`

Season year used by automation loops.

Example:

```bash
export TRUESKILL_SEASON_YEAR=2026
```

#### `TRUESKILL_DB_ADMIN_URI`

Optional admin/superuser DSN used when the main database does not exist and the API needs to auto-create it.

#### `TRUESKILL_DB_ADMIN_DB`

Optional admin database name used to attempt auto-setup when `TRUESKILL_DB_ADMIN_URI` is not set.

Default:

```text
postgres
```

### `.env` fallback behavior

The code walks upward from the project directory looking for a `.env` file and loads values as defaults if they are not already set in the environment.

That means local development can work without exporting every variable manually, as long as the `.env` file exists and contains valid `KEY=value` lines.

---

## Running the API

### Option 1: direct Python launch

```bash
python3 trueskill_api_v4.py
```

### Option 2: Uvicorn

```bash
uvicorn trueskill_api_v4:app --host 0.0.0.0 --port 5000 --workers 1
```

### Base URL

```text
http://localhost:5000
```

### Important

Use:

```text
--workers 1
```

This is necessary because ratings are maintained in shared in-memory state.

---

## TrueSkill Model Settings

The service initializes the TrueSkill environment with the following values:

- `mu = 25.0`
- `sigma = 9.0`
- `beta = 3.0`
- `tau = 0.04`
- `draw_probability = 1.0 / 1250.0`

### Meaning of these settings

- **mu**: default starting skill estimate
- **sigma**: starting uncertainty
- **beta**: affects how strongly skill differences translate into win probability
- **tau**: skill drift factor over time
- **draw_probability**: configured to make ties extremely rare

### Conservative rating used throughout the API

For many responses, the service calculates:

```text
conservative_mu_3sigma = mu - 3 * sigma
```

This acts as a lower-confidence ranking metric and is used for leaderboard sorting.

### Confidence percentage

The service also computes:

```text
confidence_percent = 100 * (1 - (sigma / initial_sigma)^2)
```

where `initial_sigma` is the environment sigma, currently `9.0`.

This is not an official TrueSkill output; it is an application-specific measure of how much uncertainty has reduced.

---

## Data Flow

### 1. Startup

On startup, the service:

1. Connects to PostgreSQL.
2. Creates missing tables and indexes.
3. Loads `team_current` into memory.
4. Starts two background automation loops.

### 2. Update by event

`POST /update` with an `event_key`:

- fetches `event/{event_key}/matches/simple`
- uses ETag caching
- skips already-recorded matches for that event
- updates ratings only for new played matches
- writes match, history, and current ratings to PostgreSQL

### 3. Update by year

`POST /update` with a `year`:

- fetches `events/{year}/simple`
- fetches each event's team list and match list
- rebuilds the entire season state for fetched events
- clears old rows for those events from `team_history` and `match_results`
- clears and rebuilds `team_current`
- reseeds all discovered teams with baseline ratings before replaying matches in chronological order

### 4. Manual pushes

`POST /push_results`:

- applies client-provided matches directly
- stores them as synthetic `manual_*` matches
- updates team history and current ratings

### 5. Broadcasting

After mutating operations complete, the service broadcasts the new leaderboard to all connected WebSocket clients.

---

## Persistence and Database Schema

The service creates and uses these PostgreSQL tables.

### `match_results`

Stores one row per processed match.

Columns:

- `id SERIAL PRIMARY KEY`
- `match_key TEXT UNIQUE`
- `event_key TEXT`
- `red_score INTEGER`
- `blue_score INTEGER`
- `red_teams TEXT`
- `blue_teams TEXT`
- `time INTEGER`

Notes:

- `red_teams` and `blue_teams` are stored as comma-separated team keys.
- Manual pushes use synthetic match keys like `manual_<timestamp>_<random>`.

### `team_history`

Append-only per-team rating history.

Columns:

- `id SERIAL PRIMARY KEY`
- `team TEXT`
- `mu DOUBLE PRECISION`
- `sigma DOUBLE PRECISION`
- `match_key TEXT`
- `event_key TEXT`
- `time INTEGER`

### `team_current`

Current rating snapshot used to rebuild in-memory state on startup.

Columns:

- `team TEXT PRIMARY KEY`
- `mu DOUBLE PRECISION`
- `sigma DOUBLE PRECISION`

### `etag_cache`

Stores ETags for TBA resources.

Columns:

- `resource TEXT PRIMARY KEY`
- `etag TEXT`

### Indexes created

- `idx_match_results_event ON match_results(event_key)`
- `idx_team_history_team ON team_history(team)`
- `idx_team_history_event ON team_history(event_key)`
- `idx_match_results_time ON match_results(time)`

### Database auto-setup

If `TRUESKILL_DB_URI` points to a database that does not exist, the API attempts to create it.

Behavior:

- it first tries the main DSN
- if the database is missing, it attempts admin connections
- it uses `TRUESKILL_DB_ADMIN_URI` if provided
- otherwise it tries admin DB names such as `postgres`, then `template1`

If creation fails, startup fails with a detailed error.

---

## Automation Loops

The service starts two background loops on startup.

### Active event loop

Runs every **300 seconds**.

Purpose:

- determine active events for the configured season year
- update ratings for currently active events only

How active events are determined:

- fetch `events/{year}/simple`
- include events where today's date falls between `start_date` and `end_date`
- ETag is used for the event list
- an in-memory same-day active-event cache can be reused on `304 Not Modified`

Possible cycle summary statuses:

- `completed`
- `skipped` with reasons such as:
  - `missing_tba_key`
  - `no_active_events`

### Nightly full rebuild loop

Runs once per day at a **random local time between 03:00 and 03:59**.

Purpose:

- rebuild the configured season year from TBA

The target year comes from:

- `TRUESKILL_SEASON_YEAR`, if valid
- otherwise the current local year on the server

---

## API Reference

All examples assume:

```text
http://localhost:5000
```

### 1. `GET /health`

Health check with DB status and in-memory team count.

#### Example

```bash
curl -s http://localhost:5000/health
```

#### Example response

```json
{
  "ok": true,
  "db_connected": true,
  "teams_indexed": 432
}
```

#### Notes

- `ok` is effectively the same as `db_connected`.
- If PostgreSQL is unavailable, both fields return `false`.

---

### 2. `POST /update`

Rebuild or update ratings from TBA.

You must provide **exactly one** of:

- `event_key`
- `year`

#### Event mode example

```bash
curl -s -X POST http://localhost:5000/update \
  -H "Content-Type: application/json" \
  -d '{"event_key":"2026nyro"}'
```

#### Year mode example

```bash
curl -s -X POST http://localhost:5000/update \
  -H "Content-Type: application/json" \
  -d '{"year":2026}'
```

#### Typical success response

```json
{
  "status": "rankings updated",
  "teams_indexed": 312
}
```

#### Event mode no-change response

If TBA returns `304 Not Modified` for the event matches resource:

```json
{
  "status": "no new data",
  "event_key": "2026nyro"
}
```

#### Validation errors

```json
{
  "error": "Provide either 'event_key' or 'year' (exactly one)."
}
```

#### Behavior details

**Event mode:**

- Uses `event/{event_key}/matches/simple`
- Uses ETag caching
- Skips unplayed matches
- Skips already-recorded matches for that event
- Does not wipe existing season state

**Year mode:**

- Uses `events/{year}/simple`
- For each event, fetches:
  - `event/{event}/teams/keys`
  - `event/{event}/matches/simple`
- Clears rows in `team_history` and `match_results` for fetched events
- Clears `team_current`
- Re-seeds all discovered teams with default ratings
- Replays all played matches chronologically

#### TBA auth failure example

```json
{
  "error": "TBA auth failed (401). Check TBA_AUTH_KEY and User-Agent.",
  "hint": "Set env: TBA_AUTH_KEY=... and optionally TBA_USER_AGENT=team/app/version",
  "detail": "..."
}
```

---

### 3. `POST /push_results`

Apply manual match results.

Request body must be a JSON list.

#### Example

```bash
curl -s -X POST http://localhost:5000/push_results \
  -H "Content-Type: application/json" \
  -d '[
    {
      "teams1": ["frc3173", "frc254", "frc1114"],
      "teams2": ["frc1678", "frc2056", "frc118"],
      "score1": 95,
      "score2": 82
    }
  ]'
```

#### Example response

```json
{
  "status": "results incorporated",
  "applied": 1
}
```

#### Notes

- Matches are stored with synthetic `manual_*` match keys.
- Invalid items in the list are skipped rather than hard-failing the entire request.

---

### 4. `GET /predict_team`

Return the current rating for a single team.

#### Example

```bash
curl -s "http://localhost:5000/predict_team?team=frc3173"
```

#### Example response

```json
{
  "team": "frc3173",
  "mu": 27.48,
  "sigma": 5.71,
  "conservative_mu_3sigma": 10.35,
  "confidence_percent": 59.75
}
```

#### Possible errors

Missing query parameter:

```json
{
  "error": "Missing team parameter"
}
```

Unknown team:

```json
{
  "error": "Team not found"
}
```

---

### 5. `POST /predict_match`

Predict win probabilities for two alliances of any sizes.

#### Example

```bash
curl -s -X POST http://localhost:5000/predict_match \
  -H "Content-Type: application/json" \
  -d '{
    "teams1": ["frc3173", "frc254", "frc1114"],
    "teams2": ["frc1678", "frc2056", "frc118"]
  }'
```

#### Example response

```json
{
  "team1_win_prob": 0.6132,
  "team2_win_prob": 0.3868,
  "prediction_confidence_percent": 22.64
}
```

#### Notes

- `prediction_confidence_percent` is derived from distance away from `50%`.
- Unknown teams are auto-initialized in memory if not already present, because `get_team_rating()` creates a default rating.

---

### 6. `POST /predict_batch`

Predict multiple matchups in one request.

Request body must be a JSON list.

#### Example

```bash
curl -s -X POST http://localhost:5000/predict_batch \
  -H "Content-Type: application/json" \
  -d '[
    {
      "teams1": ["frc3173", "frc254", "frc1114"],
      "teams2": ["frc1678", "frc2056", "frc118"]
    },
    {
      "teams1": ["frc1"],
      "teams2": ["frc2"]
    }
  ]'
```

#### Example response

```json
[
  {
    "teams1": ["frc3173", "frc254", "frc1114"],
    "teams2": ["frc1678", "frc2056", "frc118"],
    "team1_win_prob": 0.6132,
    "team2_win_prob": 0.3868
  },
  {
    "teams1": ["frc1"],
    "teams2": ["frc2"],
    "team1_win_prob": 0.5,
    "team2_win_prob": 0.5
  }
]
```

#### Notes

- Individual invalid matchup items return inline error objects rather than aborting the full batch.

---

### 7. `POST /recalculate`

Recomputes derived values and writes a JSON snapshot.

Optional body:

```json
{
  "source": "memory"
}
```

or

```json
{
  "source": "json"
}
```

#### Example using current memory

```bash
curl -s -X POST http://localhost:5000/recalculate \
  -H "Content-Type: application/json" \
  -d '{"source":"memory"}'
```

#### Example response

```json
{
  "status": "recalculated",
  "source": "memory",
  "teams_indexed": 312,
  "file": "trueskill_data.json",
  "saved_teams_indexed": 312,
  "env": {
    "mu": 25.0,
    "sigma": 9.0,
    "beta": 3.0,
    "tau": 0.04,
    "draw_probability": 0.0008
  },
  "context": {
    "event_key": null,
    "year": 2026,
    "teams_indexed": 312
  }
}
```

#### Behavior details

- If `source=json`, the API attempts to read a snapshot first and rebuild `TEAM_RATINGS` from it.
- If JSON contains `meta.env`, the code may reinitialize the TrueSkill environment from the snapshot.
- It then writes a fresh snapshot back out.

#### Important implementation note

When `source=json`, the code defaults the input path to:

```text
DB/JSON/trueskill_data.json
```

unless `TRUESKILL_DATA_PATH` is set.

When saving, the output path default is:

```text
trueskill_data.json
```

unless `TRUESKILL_DATA_PATH` is set.

Setting `TRUESKILL_DATA_PATH` avoids this mismatch.

---

### 8. `POST /upload_data`

Writes the current in-memory ratings to JSON.

#### Example

```bash
curl -s -X POST http://localhost:5000/upload_data
```

#### Example response

```json
{
  "status": "saved",
  "file": "trueskill_data.json",
  "teams_indexed": 312
}
```

---

### 9. `POST /load_data`

Loads ratings from JSON into memory only.

Optional body:

```json
{
  "path": "trueskill_data.json",
  "use_env_from_json": true
}
```

#### Example

```bash
curl -s -X POST http://localhost:5000/load_data \
  -H "Content-Type: application/json" \
  -d '{"path":"trueskill_data.json","use_env_from_json":true}'
```

#### Example response

```json
{
  "status": "loaded",
  "file": "trueskill_data.json",
  "use_env_from_json": true,
  "teams_indexed": 312,
  "context": {
    "event_key": null,
    "year": 2026
  }
}
```

#### Important note

This endpoint **does not update PostgreSQL tables**. It only replaces in-memory `TEAM_RATINGS`.

That means endpoints such as:

- `/team_history/{team_id}`
- `/match/{match_key}/analysis`
- `/event/{event_key}/upsets`

still use database-backed data that may not match the newly loaded memory state.

---

### 10. `GET /leaderboard`

Returns all known teams sorted by `conservative_mu_3sigma` descending.

#### Example

```bash
curl -s http://localhost:5000/leaderboard
```

#### Example response

```json
{
  "teams": [
    {
      "team_key": "frc254",
      "mu": 33.12,
      "sigma": 3.41,
      "conservative_mu_3sigma": 22.89,
      "confidence_percent": 85.64
    },
    {
      "team_key": "frc1678",
      "mu": 32.88,
      "sigma": 3.55,
      "conservative_mu_3sigma": 22.23,
      "confidence_percent": 84.43
    }
  ],
  "teams_indexed": 312
}
```

---

### 11. `GET /team_history/{team_id}`

Returns a team's rating history from PostgreSQL.

#### Example

```bash
curl -s http://localhost:5000/team_history/frc3173
```

#### Example response

```json
{
  "team": "frc3173",
  "history": [
    {
      "mu": 25.92,
      "sigma": 8.17,
      "conservative": 1.41,
      "time": 1700000000,
      "match_key": "2026nyro_qm1",
      "event_key": "2026nyro"
    },
    {
      "mu": 26.48,
      "sigma": 7.61,
      "conservative": 3.65,
      "time": 1700000300,
      "match_key": "2026nyro_qm7",
      "event_key": "2026nyro"
    }
  ]
}
```

---

### 12. `GET /compare`

Compare two teams directly.

Query params:

- `team1`
- `team2`

#### Example

```bash
curl -s "http://localhost:5000/compare?team1=frc3173&team2=frc254"
```

#### Example response

```json
{
  "team1": {
    "team_key": "frc3173",
    "mu": 27.48,
    "sigma": 5.71,
    "conservative_mu_3sigma": 10.35,
    "confidence_percent": 59.75
  },
  "team2": {
    "team_key": "frc254",
    "mu": 33.12,
    "sigma": 3.41,
    "conservative_mu_3sigma": 22.89,
    "confidence_percent": 85.64
  },
  "team1_win_prob": 0.274,
  "team2_win_prob": 0.726,
  "history1": [
    {"mu": 25.92, "sigma": 8.17, "time": 1700000000}
  ],
  "history2": [
    {"mu": 29.87, "sigma": 5.10, "time": 1700000000}
  ]
}
```

---

### 13. `POST /picklist`

Generate candidate 3-team alliances around a target team.

Request body:

```json
{
  "target_team": "frc3173",
  "taken": ["frc254"],
  "playoff_alliances": [
    ["frc1678", "frc2056", "frc118"],
    ["frc1114", "frc148", "frc1323"]
  ]
}
```

#### Example

```bash
curl -s -X POST http://localhost:5000/picklist \
  -H "Content-Type: application/json" \
  -d '{
    "target_team":"frc3173",
    "taken":["frc254"],
    "playoff_alliances":[
      ["frc1678","frc2056","frc118"]
    ]
  }'
```

#### Example response

```json
[
  {
    "alliance": ["frc3173", "frc1114", "frc148"],
    "confidence_percent": 72.43,
    "win_prob_avg": 0.581
  },
  {
    "alliance": ["frc3173", "frc1323", "frc3538"],
    "confidence_percent": 68.12,
    "win_prob_avg": 0.544
  }
]
```

#### Sorting behavior

- If `playoff_alliances` is provided, results are sorted by `win_prob_avg` descending.
- Otherwise they are sorted by `confidence_percent` descending.

#### Notes

- The endpoint generates all 2-team combinations around the target team.
- This can become expensive if the pool of available teams is large.

---

### 14. `POST /teams/compare`

Compare multiple teams at once.

#### Example

```bash
curl -s -X POST http://localhost:5000/teams/compare \
  -H "Content-Type: application/json" \
  -d '{"teams":["frc3173","frc254","frc1678"]}'
```

#### Example response

```json
{
  "teams": [
    {
      "team_key": "frc3173",
      "mu": 27.48,
      "sigma": 5.71,
      "conservative_mu_3sigma": 10.35,
      "confidence_percent": 59.75,
      "history": [
        {
          "mu": 25.92,
          "sigma": 8.17,
          "match_key": "2026nyro_qm1",
          "event_key": "2026nyro",
          "time": 1700000000
        }
      ]
    },
    {
      "team_key": "frc254",
      "mu": 33.12,
      "sigma": 3.41,
      "conservative_mu_3sigma": 22.89,
      "confidence_percent": 85.64,
      "history": []
    }
  ]
}
```

For unknown teams, an item looks like:

```json
{
  "team_key": "frc99999",
  "error": "Team not found"
}
```

---

### 15. `GET /match/{match_key}/analysis`

Analyze a stored match using **current** ratings.

#### Example

```bash
curl -s http://localhost:5000/match/2026nyro_qm1/analysis
```

#### Example response

```json
{
  "match_key": "2026nyro_qm1",
  "teams_red": ["frc3173", "frc254", "frc1114"],
  "teams_blue": ["frc1678", "frc2056", "frc118"],
  "predicted_red_win_prob": 0.6132,
  "predicted_blue_win_prob": 0.3868
}
```

#### Notes

- This endpoint does **not** reproduce the historical pre-match prediction at event time.
- It analyzes the match using the **current** `TEAM_RATINGS` loaded in memory.

---

### 16. `GET /event/{event_key}/upsets`

Returns stored matches from an event where the predicted winner did not match the actual winner.

#### Example

```bash
curl -s http://localhost:5000/event/2026nyro/upsets
```

#### Example response

```json
{
  "event_key": "2026nyro",
  "upsets": [
    {
      "match_key": "2026nyro_qm24",
      "teams_red": ["frc3173", "frc1", "frc2"],
      "teams_blue": ["frc3", "frc4", "frc5"],
      "score_red": 75,
      "score_blue": 88,
      "predicted_red_win_prob": 0.672
    }
  ]
}
```

#### Notes

- Like `/match/{match_key}/analysis`, this uses **current ratings**, not historical point-in-time ratings.

---

### 17. `POST /predict_alliance`

Predict a strict 3v3 alliance matchup.

#### Example

```bash
curl -s -X POST http://localhost:5000/predict_alliance \
  -H "Content-Type: application/json" \
  -d '{
    "teams_red": ["frc3173", "frc254", "frc1114"],
    "teams_blue": ["frc1678", "frc2056", "frc118"]
  }'
```

#### Example response

```json
{
  "red_win_probability": 0.6132,
  "blue_win_probability": 0.3868
}
```

#### Validation failure

```json
{
  "error": "Provide two lists of 3 team keys each"
}
```

---

## WebSocket Live Updates

### Endpoint

```text
/ws
```

### Behavior

- The server accepts the WebSocket connection and stores the client.
- On leaderboard-changing operations, it broadcasts a JSON message.
- The current code keeps the connection alive by awaiting `receive_text()` in a loop.

That means your client should be prepared to:

- receive server messages
- optionally send a small keepalive text message periodically

### Broadcast payload format

```json
{
  "type": "leaderboard_update",
  "data": {
    "teams": [
      {
        "team_key": "frc254",
        "mu": 33.12,
        "sigma": 3.41,
        "conservative_mu_3sigma": 22.89,
        "confidence_percent": 85.64
      }
    ],
    "teams_indexed": 312
  }
}
```

### Minimal JavaScript example

```javascript
const ws = new WebSocket("ws://localhost:5000/ws");

ws.onopen = () => {
  console.log("connected");
  setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send("ping");
    }
  }, 30000);
};

ws.onmessage = (event) => {
  const payload = JSON.parse(event.data);
  if (payload.type === "leaderboard_update") {
    console.log(payload.data.teams_indexed);
    console.log(payload.data.teams[0]);
  }
};

ws.onclose = () => {
  console.log("disconnected");
};
```

---

## JSON Snapshot Format

Endpoints such as `/upload_data` and `/recalculate` write JSON in this shape:

```json
{
  "meta": {
    "generated_at": "2026-03-12T12:34:56.789012+00:00",
    "source": "The Blue Alliance (processed locally)",
    "env": {
      "mu": 25.0,
      "sigma": 9.0,
      "beta": 3.0,
      "tau": 0.04,
      "draw_probability": 0.0008
    },
    "context": {
      "event_key": null,
      "year": 2026,
      "teams_indexed": 312
    }
  },
  "teams": [
    {
      "team_key": "frc3173",
      "mu": 27.48,
      "sigma": 5.71,
      "conservative_mu_3sigma": 10.35,
      "confidence_percent": 59.75
    }
  ]
}
```

### Notes

- `teams` is the authoritative section for restoring ratings.
- `meta.env` may be used to reconstruct the TrueSkill environment on load.
- `conservative_mu_3sigma` and `confidence_percent` are derived values included for convenience.

---

## Operational Notes and Caveats

### 1. PostgreSQL is the main persistent store

The application treats PostgreSQL as the persistent source of truth for matches/history/current ratings.

### 2. `load_data` is memory-only

Loading from JSON does not repopulate database tables.

### 3. Single-worker deployment is important

Do not scale this with multiple workers unless you redesign shared state.

### 4. Unknown teams may be auto-created in some prediction endpoints

`get_team_rating()` creates a default TrueSkill entry if a team key is missing. That is helpful for prediction convenience, but it can also introduce previously unseen teams into memory.

### 5. Event analysis endpoints use current ratings

`/match/{match_key}/analysis` and `/event/{event_key}/upsets` use whatever ratings are currently in memory, not the rating state at the time the match was originally played.

### 6. Margin-of-victory heuristic

After a normal match update, the code applies extra TrueSkill updates for blowouts:

- if winner/loser score ratio `>= 2.3`, apply 1 extra update
- if ratio `>= 3.2`, apply 2 extra updates total

This is an application-level heuristic layered on top of standard TrueSkill.

### 7. Unplayed matches are skipped

Matches with missing or negative scores are not applied.

### 8. CORS is fully open

The API currently allows:

- all origins
- all methods
- all headers
- credentials enabled

That is convenient for development, but you may want to tighten it for your production environment.

---

## Example Workflow

### Initial season rebuild

```bash
curl -X POST http://localhost:5000/update \
  -H "Content-Type: application/json" \
  -d '{"year":2026}'
```

### Check health

```bash
curl http://localhost:5000/health
```

### View the leaderboard

```bash
curl http://localhost:5000/leaderboard
```

### Predict a matchup

```bash
curl -X POST http://localhost:5000/predict_match \
  -H "Content-Type: application/json" \
  -d '{
    "teams1": ["frc3173", "frc254", "frc1114"],
    "teams2": ["frc1678", "frc2056", "frc118"]
  }'
```

### Save a JSON snapshot

```bash
curl -X POST http://localhost:5000/upload_data
```

### Analyze a single team

```bash
curl "http://localhost:5000/predict_team?team=frc3173"
```

---

## Troubleshooting

### TBA auth errors

Symptoms:

- `/update` returns TBA auth failed
- active event automation is skipped or fails

Check:

- `TBA_AUTH_KEY`
- `VITE_TBA_API_KEY` fallback if using frontend-style env names
- `TBA_USER_AGENT`

### Database connection failure

Symptoms:

- app fails on startup
- `/health` shows `db_connected: false`

Check:

- `TRUESKILL_DB_URI`
- PostgreSQL host, port, credentials, database existence
- optional admin URI if you want auto-creation to work

### Ratings appear inconsistent after `/load_data`

Cause:

- memory state was replaced from JSON
- database-backed endpoints still reference PostgreSQL data

Fix:

- use `/update` or `/push_results` to rebuild/persist correctly
- or treat `/load_data` as a temporary memory override only

### WebSocket closes unexpectedly

Cause:

- current implementation waits on `receive_text()`

Fix:

- send periodic text keepalives such as `ping`

### Duplicate state across workers

Cause:

- app started with multiple Uvicorn workers

Fix:

- run with `--workers 1`

---

## License Notice

From the source file:

> TrueSkill is the exclusive property of Microsoft Corporation.
>
> Used under the terms of the TrueSkill license:
> Microsoft permits only Xbox Live games or non-commercial projects to use TrueSkill(TM).
> If your project is commercial, you should find another rating system.
>
> This TrueSkill project is opened under the BSD license but the
> TrueSkill(TM) brand is not.

Some code snippets were taken from Stack Overflow and The Blue Alliance's API is compleyley seperate from this API.

---

## Summary

This version of the API provides:

- season/event ingestion from TBA
- PostgreSQL-backed persistence
- in-memory high-speed prediction
- live WebSocket leaderboard updates
- manual result injection
- team history and comparison endpoints
- alliance picklist generation
- upset and match analysis tools
- JSON snapshot import/export
- automated active-event refreshes and nightly rebuilds

For production use, the most important deployment rules are:

- configure PostgreSQL correctly
- configure the TBA key and User-Agent
- run with a single worker
- understand the difference between database-backed history and memory-only JSON loads