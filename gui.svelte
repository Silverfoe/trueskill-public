<script>
  // State variables for form inputs and results
  let updateMode = 'year';       // 'event' or 'year' for the Update form
  let eventKey = '';
  let year = '';
  let updateResult = null;
  let updateInProgress = false;        // NEW: indicates if update fetch is in progress

  let pushMatches = [
    { teams1: '', teams2: '', score1: '', score2: '' }
  ];
  let pushResponse = null;

  let teamQuery = '';
  let teamResult = null;
  let lastTeamQueries = [];            // NEW: store last 5 team query results for history

  let matchTeams1 = '';
  let matchTeams2 = '';
  let matchProbResult = null;

  let batchMatches = [
    { teams1: '', teams2: '' }
  ];
  let batchResults = null;

  // Leaderboard feature state
  let leaderboardInput = '';           // NEW: stores the year or event key input for leaderboard
  let leaderboardResults = null;       // NEW: will hold the leaderboard data (array of teams)
  let leaderboardTitle = '';           // NEW: context label (event key or year) for display
  let leaderboardLoading = false;      // NEW: indicates if leaderboard calculation is in progress
  let leaderboardError = null;         // NEW: error message for leaderboard (if any)

  // Functions to manage dynamic lists (add/remove matches for push and batch)
  function addPushMatch() {
    pushMatches = [...pushMatches, { teams1: '', teams2: '', score1: '', score2: '' }];
  }
  function removePushMatch(index) {
    pushMatches = pushMatches.filter((_, i) => i !== index);
  }
  function addBatchMatch() {
    batchMatches = [...batchMatches, { teams1: '', teams2: '' }];
  }
  function removeBatchMatch(index) {
    batchMatches = batchMatches.filter((_, i) => i !== index);
  }

  // Base URL for the local API
  const API_BASE = 'http://127.0.0.1:5000';

  // API call functions for each feature
  async function doUpdate() {
    updateResult = null;
    updateInProgress = false;
    let payload;
    if (updateMode === 'event') {
      if (!eventKey) {
        alert('Please enter an event key.');
        return;
      }
      payload = { event_key: eventKey.trim() };
    } else {
      if (!year) {
        alert('Please enter a year.');
        return;
      }
      payload = { year: parseInt(year) };
    }
    try {
      updateInProgress = true;  // indicate loading
      const res = await fetch(`${API_BASE}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) {
        // If HTTP status not OK, treat as error (use error message if provided)
        updateResult = { error: data.error || `Update failed (status ${res.status})` };
      } else {
        updateResult = data;
      }
    } catch (err) {
      updateResult = { error: err.message };
    } finally {
      updateInProgress = false;
    }
  }

  async function doPush() {
    pushResponse = null;
    // Build payload array from pushMatches input fields
    const matchesPayload = pushMatches.map(m => ({
      teams1: m.teams1.split(',').map(t => t.trim()).filter(Boolean),
      teams2: m.teams2.split(',').map(t => t.trim()).filter(Boolean),
      score1: Number(m.score1),
      score2: Number(m.score2)
    }));
    try {
      const res = await fetch(`${API_BASE}/push_results`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(matchesPayload)
      });
      pushResponse = await res.json();
    } catch (err) {
      pushResponse = { error: err.message };
    }
  }

  async function doQueryTeam() {
    teamResult = null;
    if (!teamQuery) {
      alert('Please enter a team key (e.g. frc254).');
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/predict_team?team=${encodeURIComponent(teamQuery.trim())}`);
      const data = await res.json();
      if (!res.ok) {
        // If team not found or other error
        teamResult = { error: data.error || `Query failed (status ${res.status})` };
      } else {
        teamResult = data;
        // Maintain history of last 5 queries (newest first)
        lastTeamQueries.unshift({ ...data });  // store a copy of the result
        if (lastTeamQueries.length > 5) {
          lastTeamQueries.pop();               // keep only last 5
        }
      }
    } catch (err) {
      teamResult = { error: err.message };
    }
  }

  async function doPredictMatch() {
    matchProbResult = null;
    if (!matchTeams1 || !matchTeams2) {
      alert('Please enter team lists for both alliances.');
      return;
    }
    const payload = {
      teams1: matchTeams1.split(',').map(t => t.trim()).filter(Boolean),
      teams2: matchTeams2.split(',').map(t => t.trim()).filter(Boolean)
    };
    try {
      const res = await fetch(`${API_BASE}/predict_match`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      matchProbResult = await res.json();
    } catch (err) {
      matchProbResult = { error: err.message };
    }
  }

  async function doBatchPredict() {
    batchResults = null;
    const batchPayload = batchMatches.map(m => ({
      teams1: m.teams1.split(',').map(t => t.trim()).filter(Boolean),
      teams2: m.teams2.split(',').map(t => t.trim()).filter(Boolean)
    }));
    try {
      const res = await fetch(`${API_BASE}/predict_batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(batchPayload)
      });
      batchResults = await res.json();
    } catch (err) {
      batchResults = [{ error: err.message }];
    }
  }

  // NEW: Fetch and display leaderboard rankings for a given event or year
  async function doLeaderboard() {
    leaderboardResults = null;
    leaderboardError = null;
    leaderboardTitle = '';
    const input = leaderboardInput.trim();
    if (!input) {
      alert('Please enter a year or event key.');
      return;
    }
    // Determine if input is a year (4-digit) or an event key
    const isYear = /^\d{4}$/.test(input);
    const payload = isYear ? { year: parseInt(input) } : { event_key: input };
    leaderboardTitle = isYear ? `Year ${input}` : `Event ${input}`;
    try {
      leaderboardLoading = true;
      // First, update ratings for the given context (year or event)
      let res = await fetch(`${API_BASE}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      let data = await res.json();
      if (!res.ok) {
        // If update failed, handle error
        throw new Error(data.error || `Failed to update ratings (status ${res.status})`);
      }
      // Next, fetch the sorted team ratings for the current in-memory teams
      res = await fetch(`${API_BASE}/leaderboard`);
      data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `Failed to fetch leaderboard (status ${res.status})`);
      }
      leaderboardResults = data.teams;
      // (The API returns all teams in memory sorted by rating)
    } catch (err) {
      leaderboardError = err.message;
    } finally {
      leaderboardLoading = false;
    }
  }
</script>

<style>
  body {
    font-family: Arial, sans-serif;
    background: #f0f0f0;
    margin: 1rem;
  }
  .section {
    background: #fff;
    border: none;
    border-radius: 5px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    padding: 1rem;
    margin: 1rem 0;
  }
  .section h2 {
    margin-top: 0;
    color: #333;
  }
  fieldset {
    border: 1px solid #ccc;
    border-radius: 4px;
    margin: 0.5rem 0;
    padding: 0.8rem;
  }
  fieldset legend {
    font-weight: bold;
    padding: 0 0.5rem;
  }
  .error {
    color: red;
    font-weight: bold;
  }
  input {
    padding: 0.3rem 0.5rem;
    margin: 0.3rem 0.5rem 0.3rem 0;
    border: 1px solid #ccc;
    border-radius: 4px;
    max-width: 100%;
    box-sizing: border-box;
  }
  button {
    padding: 0.4rem 0.8rem;
    margin: 0.3rem 0.5rem 0.3rem 0;
    border: none;
    border-radius: 4px;
    background-color: #0077cc;
    color: #fff;
    cursor: pointer;
  }
  button:hover {
    background-color: #005fa3;
  }
  /* Remove default left margin on certain buttons for consistent alignment */
  /* (Previously, button[type="button"] had margin-left: 0.5rem) */
  button[type="button"] {
    margin-left: 0;
  }
  /* Responsive layout: use flexbox for match input groups and allow wrapping */
  .section fieldset {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
  }
  .section fieldset input {
    /* space out inputs within fieldset */
    margin-right: 0.5rem;
  }
  .section fieldset button {
    /* small top margin to align with input fields if wrapped */
    margin-top: 0.3rem;
  }
  /* Leaderboard table styling */
  table.leaderboard-table {
    width: 100%;
    border-collapse: collapse;
  }
  table.leaderboard-table th, table.leaderboard-table td {
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid #eee;
  }
  table.leaderboard-table th {
    text-align: left;
    background: #fafafa;
    border-bottom: 1px solid #ccc;
  }
  table.leaderboard-table td.num {
    text-align: right;
    font-family: monospace;
  }
  table.leaderboard-table tr:nth-child(even) {
    background: #f9f9f9;
  }
  /* Disable text selection on loading messages (optional) */
  .loading {
    font-style: italic;
    color: #555;
  }
</style>

<div class="section">
  <h2>Update Ratings</h2>
  <div>
    <label>
      <input type="radio" bind:group={updateMode} value="event">
      Update by Event
    </label>
    <label>
      <input type="radio" bind:group={updateMode} value="year">
      Update by Year
    </label>
  </div>
  {#if updateMode === 'event'}
    <input type="text" placeholder="Event Key (e.g. 2025nyrr)" bind:value={eventKey}>
  {:else}
    <input type="number" placeholder="Year (e.g. 2025)" bind:value={year}>
  {/if}
  <button on:click={doUpdate} disabled={updateInProgress}>Update</button>
  {#if updateInProgress}
    <p class="loading">Updating data, please wait...</p>
  {/if}
  {#if updateResult}
    {#if updateResult.error}
      <p class="error">Error: {updateResult.error}</p>
    {:else}
      <p>
        {updateResult.status}
        {#if updateResult.event_key} for event {updateResult.event_key}{/if}
        {#if updateResult.year} for year {updateResult.year}{/if}
        (Teams indexed: {updateResult.teams_indexed})
      </p>
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Push Match Results</h2>
  {#each pushMatches as match, i}
    <fieldset>
      <legend>Match {i+1}</legend>
      <input type="text" placeholder="Alliance 1 teams (comma-separated)" bind:value={match.teams1}>
      <input type="text" placeholder="Alliance 2 teams (comma-separated)" bind:value={match.teams2}>
      <input type="number" placeholder="Alliance 1 score" bind:value={match.score1}>
      <input type="number" placeholder="Alliance 2 score" bind:value={match.score2}>
      {#if pushMatches.length > 1}
        <button on:click={() => removePushMatch(i)} type="button">Remove</button>
      {/if}
    </fieldset>
  {/each}
  <button on:click={addPushMatch} type="button">Add Another Match</button>
  <button on:click={doPush}>Submit Results</button>
  {#if pushResponse}
    {#if pushResponse.error}
      <p class="error">Error: {pushResponse.error}</p>
    {:else}
      <p>{pushResponse.status} (Applied {pushResponse.applied} results)</p>
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Query Team Rating</h2>
  <input type="text" placeholder="Team key (e.g. frc3173)" bind:value={teamQuery}>
  <button on:click={doQueryTeam}>Get Rating</button>
  {#if teamResult}
    {#if teamResult.error}
      <p class="error">Error: {teamResult.error}</p>
    {:else}
      <p>Team {teamResult.team.toUpperCase()}: μ = {teamResult.mu.toFixed(2)}, σ = {teamResult.sigma.toFixed(2)}</p>
      <p>Conservative rating (μ−3σ): {(teamResult.mu - 3 * teamResult.sigma).toFixed(2)}</p>
      <p>Rating confidence: {teamResult.confidence_percent.toFixed(2)}%</p>
    {/if}
  {/if}
  {#if lastTeamQueries.length > 0}
    <h3>Recent Queries</h3>
    <ul>
      {#each lastTeamQueries as q, idx}
        <li>
          Team {q.team.toUpperCase()}: 
          μ = {Number(q.mu).toFixed(2)}, 
          σ = {Number(q.sigma).toFixed(2)},
          μ−3σ = {Number(q.conservative_mu_3sigma).toFixed(2)},
          conf = {Number(q.confidence_percent).toFixed(2)}%
        </li>
      {/each}
    </ul>
  {/if}
</div>

<div class="section">
  <h2>Predict Match Outcome</h2>
  <input type="text" placeholder="Alliance 1 teams (comma-separated)" bind:value={matchTeams1}>
  <input type="text" placeholder="Alliance 2 teams (comma-separated)" bind:value={matchTeams2}>
  <button on:click={doPredictMatch}>Predict</button>
  {#if matchProbResult}
    {#if matchProbResult.error}
      <p class="error">Error: {matchProbResult.error}</p>
    {:else}
      <p>Alliance 1 Win Probability: {(matchProbResult.team1_win_prob * 100).toFixed(2)}%</p>
      <p>Alliance 2 Win Probability: {(matchProbResult.team2_win_prob * 100).toFixed(2)}%</p>
      <p>Prediction confidence: {matchProbResult.prediction_confidence_percent.toFixed(2)}%</p>
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Batch Match Predictions</h2>
  {#each batchMatches as match, j}
    <fieldset>
      <legend>Match {j+1}</legend>
      <input type="text" placeholder="Alliance 1 teams (comma-separated)" bind:value={match.teams1}>
      <input type="text" placeholder="Alliance 2 teams (comma-separated)" bind:value={match.teams2}>
      {#if batchMatches.length > 1}
        <button on:click={() => removeBatchMatch(j)} type="button">Remove</button>
      {/if}
    </fieldset>
  {/each}
  <button on:click={addBatchMatch} type="button">Add Another Match</button>
  <button on:click={doBatchPredict}>Predict Batch</button>
  {#if batchResults}
    {#each batchResults as result, k}
      {#if result.error}
        <p class="error">Match {k+1}: Error: {result.error}</p>
      {:else}
        <p>
          Match {k+1}: 
          Alliance 1 Win = {(result.team1_win_prob * 100).toFixed(2)}%, 
          Alliance 2 Win = {(result.team2_win_prob * 100).toFixed(2)}%
          {#if result.prediction_confidence_percent !== undefined}
            , Confidence = {result.prediction_confidence_percent.toFixed(2)}%
          {/if}
        </p>
      {/if}
    {/each}
  {/if}
</div>

<div class="section">
  <h2>Leaderboard</h2>
  <input type="text" placeholder="Year or Event Key (e.g. 2025 or 2025miket)" bind:value={leaderboardInput}>
  <button on:click={doLeaderboard} disabled={leaderboardLoading}>Generate Leaderboard</button>
  {#if leaderboardLoading}
    <p class="loading">Calculating rankings, please wait...</p>
  {/if}
  {#if leaderboardError}
    <p class="error">Error: {leaderboardError}</p>
  {/if}
  {#if leaderboardResults}
    <p><strong>Leaderboard for {leaderboardTitle}</strong> – Total Teams: {leaderboardResults.length}</p>
    <!-- Leaderboard table -->
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Team</th>
          <th>μ</th>
          <th>σ</th>
          <th>μ−3σ</th>
          <th>Conf%</th>
        </tr>
      </thead>
      <tbody>
        {#each leaderboardResults as entry, idx}
          <tr>
            <td>{entry.team_key ? entry.team_key.toUpperCase() : ''}</td>
            <td class="num">{Number(entry.mu).toFixed(2)}</td>
            <td class="num">{Number(entry.sigma).toFixed(2)}</td>
            <td class="num">{Number(entry.conservative_mu_3sigma).toFixed(2)}</td>
            <td class="num">{Number(entry.confidence_percent).toFixed(2)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>
