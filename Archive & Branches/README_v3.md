# TrueSkill Robotics Prediction API

**Local-first ratings & match predictions for FRC, powered by Microsoft TrueSkill and The Blue Alliance**

---

## Overview

This API runs **locally** on `127.0.0.1:5000`, ingests official FRC match data from **The Blue Alliance (TBA)**, computes **TrueSkill** ratings for every team, and serves **win-probability** predictions for any alliance matchup (single or batch). It’s designed to be clean, fast, and integration-friendly—returning plain JSON so your apps, scripts, or dashboards can consume it directly.

### Highlights

* **Local only** by default (no public exposure).
* **Full-season ingestion** (`/update` with `{"year": YYYY}`) processes **all events** and **all matches** in that year.
* **Accurate, analytical predictions** using the canonical TrueSkill CDF.
* **Stateful when needed**: `/upload_data` saves to `trueskill_data.json`, `/load_data` restores—no re-fetch required.
* **Live updates** via `/push_results` without hitting TBA.
* **Informative outputs**: μ, σ, **conservative rating** (μ − 3σ), **confidence%** (from σ shrinkage), and prediction confidence for a matchup.

---

## Quickstart

```bash
python -m venv .venv
. .venv/bin/activate         # Windows: .\.venv\Scripts\activate
pip install flask flask-cors requests trueskill

# REQUIRED: TBA read key
export TBA_AUTH_KEY="YOUR_TBA_READ_KEY"   # Windows PowerShell: $env:TBA_AUTH_KEY="YOUR_TBA_READ_KEY"

# Optional: choose a save path
# export TRUESKILL_DATA_PATH="/path/to/trueskill_data.json"

python trueskill_api_v2.py
# → API on http://127.0.0.1:5000
```

Check health:

```bash
curl -s http://127.0.0.1:5000/health
# {"ok": true, "teams_indexed": 0}
```

---

## Data Model (mental map)

* **TEAM_RATINGS** (memory): `{ "frc####": Rating(mu, sigma), ... }`
* **Snapshot file** (`trueskill_data.json`): includes metadata (environment, context) + all teams with μ, σ, conservative μ, confidence%.
* **Context** tracks last `event_key` or `year` used to build the snapshot.

---

## The Math (precise & practical)

### TrueSkill fundamentals

Each team’s skill is modeled as a Gaussian:

* **μ (mu)**: estimated skill
* **σ (sigma)**: uncertainty in that estimate

Default prior (TrueSkill environment):
$
\mu_0 = 25,\quad \sigma_0 = \frac{25}{3} \approx 8.33
$
As matches occur, winners’ μ tends to increase, losers’ μ decreases, and **σ shrinks** (more data → more certainty).

### Alliance vs Alliance — win probability

Let Alliance 1 teams be (i \in A_1), Alliance 2 teams be (j \in A_2).

* Aggregate means:
  $
  \mu_1 = \sum_{i \in A_1} \mu_i,\quad \mu_2 = \sum_{j \in A_2} \mu_j
  $
* Aggregate variance from all players:
  $
  \Sigma_{\sigma^2} = \sum_{k \in A_1 \cup A_2} \sigma_k^2
  $
* Let ( $N = |A_1| + |A_2|$ ) and ( $\beta$ ) be the environment’s performance variance scale.

Effective standard deviation for the skill difference:
$
\text{denom} = \sqrt{N \cdot \beta^2 + \Sigma_{\sigma^2}}
$

Define:
$
\Delta = \frac{\mu_1 - \mu_2}{\text{denom}}
$
Then the **win probability** for Alliance 1 is:
$
P(\text{A1 wins}) = \Phi(\Delta)
$
where ( $\Phi$ ) is the standard normal CDF. The model returns:
$
\text{team1\_win\_prob} = \Phi(\Delta),\quad \text{team2\_win\_prob} = 1 - \Phi(\Delta)
$
**Interpretation:** Bigger ( $\mu_1 - \mu_2$ ), or smaller uncertainty (`denom`), yields more decisive probabilities.

### Conservative rating & confidence

* **Conservative skill**:
  $
  \text{conservative\_mu\_3sigma} = \mu - 3\sigma
  $
  A strict lower-bound estimate; useful for risk-aware rankings.

* **Confidence percent** (how much uncertainty has been reduced vs prior):
  $
  \text{confidence\%} = 100 \times \left(1 - \left(\frac{\sigma}{\sigma_0}\right)^2\right)
  $
  Starts near 0% with no data; climbs toward 100% as σ shrinks.

---

## Endpoints

**Base URL:** `http://127.0.0.1:5000`

| Method | Path             | Summary                                                                                          |
| -----: | ---------------- | ------------------------------------------------------------------------------------------------ |
|    GET | `/health`        | Service check; returns `teams_indexed`                                                           |
|   POST | `/update`        | Rebuild ratings from TBA by **event** (`{"event_key":"YYYYxxxx"}`) or **year** (`{"year":YYYY}`) |
|   POST | `/push_results`  | Incrementally apply match results you provide (no TBA call)                                      |
|    GET | `/predict_team`  | Return μ, σ, conservative μ, confidence% for one team                                            |
|   POST | `/predict_match` | Win probability for one matchup (also returns a prediction confidence%)                          |
|   POST | `/predict_batch` | Win probabilities for many matchups                                                              |
|   POST | `/upload_data`   | Save current ratings + metadata to `trueskill_data.json`                                         |
|   POST | `/load_data`     | Load ratings from `trueskill_data.json` into memory                                              |
|   POST | `/recalculate`   | Refresh derived fields; optionally load from JSON first                                          |

### `/health` — GET

```bash
curl -s http://127.0.0.1:5000/health
```

```json
{ "ok": true, "teams_indexed": 158 }
```

### `/update` — POST

Rebuild from **event** *or* **year** (fresh start).

**Event:**

```bash
curl -s -X POST http://127.0.0.1:5000/update \
  -H "Content-Type: application/json" \
  -d '{"event_key":"2025nyny"}'
```

**Year:**

```bash
curl -s -X POST http://127.0.0.1:5000/update \
  -H "Content-Type: application/json" \
  -d '{"year":2025}'
```

Response:

```json
{ "status": "rankings updated", "year": 2025, "teams_indexed": 634 }
```

**Notes**

* Gathers **all events** for the year, then **all matches** for each event; skips unplayed (scores `null`/`-1`).
* Resets in-memory ratings.
* Requires `TBA_AUTH_KEY`.

### `/push_results` — POST

Apply live results, no TBA call. Body: JSON **array** of matches:

```bash
curl -s -X POST http://127.0.0.1:5000/push_results \
  -H "Content-Type: application/json" \
  -d '[
    {"teams1":["frc254","frc1678","frc118"],"teams2":["frc1323","frc2056","frc148"],"score1":120,"score2":95},
    {"teams1":["frc1678","frc254","frc118"],"teams2":["frc2056","frc1323","frc148"],"score1":87,"score2":87}
  ]'
```

```json
{ "status": "results incorporated", "applied": 2 }
```

### `/predict_team` — GET

```bash
curl -s "http://127.0.0.1:5000/predict_team?team=frc3173"
```

```json
{
  "team": "frc3173",
  "mu": 35.67,
  "sigma": 5.19,
  "conservative_mu_3sigma": 20.10,
  "confidence_percent": 61.0
}
```

### `/predict_match` — POST

```bash
curl -s -X POST http://127.0.0.1:5000/predict_match \
  -H "Content-Type: application/json" \
  -d '{"teams1":["frc254","frc1678","frc118"],"teams2":["frc1323","frc2056","frc148"]}'
```

```json
{
  "team1_win_prob": 0.6429187735,
  "team2_win_prob": 0.3570812265,
  "prediction_confidence_percent": 28.58
}
```

> `prediction_confidence_percent = |2*P-1|*100` — how decisive the model sees this particular matchup.

### `/predict_batch` — POST

```bash
curl -s -X POST http://127.0.0.1:5000/predict_batch \
  -H "Content-Type: application/json" \
  -d '[
    {"teams1":["frc254","frc1678","frc118"],"teams2":["frc1323","frc2056","frc148"]},
    {"teams1":["frc1114","frc2056","frc1241"],"teams2":["frc33","frc217","frc910"]}
  ]'
```

```json
[
  {
    "teams1": ["frc254","frc1678","frc118"],
    "teams2": ["frc1323","frc2056","frc148"],
    "team1_win_prob": 0.6429187735,
    "team2_win_prob": 0.3570812265
  },
  {
    "teams1": ["frc1114","frc2056","frc1241"],
    "teams2": ["frc33","frc217","frc910"],
    "team1_win_prob": 0.513922184,
    "team2_win_prob": 0.486077816
  }
]
```

### `/upload_data` — POST

Save snapshot to `trueskill_data.json`:

```bash
curl -s -X POST http://127.0.0.1:5000/upload_data
```

```json
{ "status": "saved", "file": "trueskill_data.json", "teams_indexed": 634 }
```

### `/load_data` — POST

Load snapshot into memory. Optional body:

```json
{ "path": "custom/path.json", "use_env_from_json": true }
```

```bash
curl -s -X POST http://127.0.0.1:5000/load_data \
  -H "Content-Type: application/json" \
  -d '{"use_env_from_json":true}'
```

```json
{
  "status": "loaded",
  "file": "trueskill_data.json",
  "use_env_from_json": true,
  "teams_indexed": 634,
  "context": { "event_key": null, "year": 2025 }
}
```

### `/recalculate` — POST

Refresh derived fields and re-save.

* From memory (default):

```bash
curl -s -X POST http://127.0.0.1:5000/recalculate
```

* Load from JSON, then recalc + save:

```bash
curl -s -X POST http://127.0.0.1:5000/recalculate \
  -H "Content-Type: application/json" \
  -d '{"source":"json"}'
```

---

## JSON Snapshot Schema (`trueskill_data.json`)

```json
{
  "meta": {
    "generated_at": "2025-10-14T02:32:17.119288+00:00",
    "source": "The Blue Alliance (processed locally)",
    "env": {
      "mu": 25.0,
      "sigma": 8.3333333333,
      "beta": 4.1666666667,
      "tau": 0.0833333333,
      "draw_probability": 0.0
    },
    "context": {
      "event_key": "2025nyny",
      "year": null,
      "teams_indexed": 60
    }
  },
  "teams": [
    {
      "team_key": "frc1",
      "mu": 28.54,
      "sigma": 7.91,
      "conservative_mu_3sigma": 4.81,
      "confidence_percent": 9.0
    }
  ]
}
```

* **meta.env** documents the TrueSkill environment used.
* **meta.context** indicates whether this snapshot covers an event or a full year.
* **teams** contains μ, σ, conservative μ, and confidence% for each team.

---

## Recommended Workflows

**Season baseline → query → save → restore**

1. `POST /update` with `{"year": 2025}`
2. Use `/predict_match` or `/predict_batch`
3. `POST /upload_data` to persist
4. Next session: `POST /load_data` to be ready instantly

**Event + live**

1. `POST /update` with `{"event_key": "2025xxxx"}`
2. After each match: `POST /push_results`
3. Query `/predict_match` for upcoming schedule
4. `POST /upload_data` at day/event end

---

## Troubleshooting

* **Predictions stuck at 50/50**
  Ensure you’ve run `/update` or `/load_data` and `/health` shows a non-zero `teams_indexed`. Identical alliances or fully default ratings also yield 50/50.

* **`/load_data` “works” but ratings look default**
  Inspect the JSON: if μ≈25 and σ≈8.33 everywhere, it’s a default snapshot. Try `/load_data` with `{"use_env_from_json": true}` to mirror saved env.

* **Year ingestion missing teams**
  This build iterates **every event** in the year and fetches **all matches** per event. Unplayed matches (scores `null`/`-1`) are skipped by design.

* **ImportError: partially initialized module 'trueskill'**
  Don’t name your file `trueskill.py`. Use `trueskill_api_v2.py`.

---

## Design Principles

* **Deterministic rebuilds**: `/update` replays matches chronologically to produce a consistent ratings table.
* **Local-first**: Defaults to localhost; add security before exposing beyond your machine.
* **Minimal coupling**: Pure JSON in/out; easy to script, test, and integrate.
* **Explainable outputs**: μ, σ, conservative μ, confidence%, and transparent math for predictions.

---

## Changelog (most recent)

* Fixed **/load_data** to hydrate in-memory ratings reliably (with optional environment recreation).
* Correct TrueSkill probability computation in **/predict_match** and **/predict_batch**.
* **Full-year** ingestion ensures **all events** and **all teams** are included.
* Added prediction confidence% for single-match predictions.
* Robust save/load cycle via **/upload_data**, **/recalculate**, **/load_data**.

---
