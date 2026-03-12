<script>
  import { onDestroy, onMount } from 'svelte';

  // API connection state
  const LOCAL_STORAGE_API_KEY = 'trueskill_api_base';
  let apiBase = 'http://127.0.0.1:5000';
  let apiBaseDraft = apiBase;
  let apiConfigMessage = '';

  let healthLoading = false;
  let healthResult = null;
  let healthError = null;

  // WebSocket live feed state
  let ws = null;
  let wsStatus = 'disconnected';
  let wsError = null;
  let wsLastUpdate = null;

  // Update ratings state
  let updateMode = 'year';
  let eventKey = '';
  let year = '';
  let updateResult = null;
  let updateInProgress = false;

  // Push results state
  let pushMatches = [
    { teams1: '', teams2: '', score1: '', score2: '' }
  ];
  let pushResponse = null;

  // Team query state
  let teamQuery = '';
  let teamResult = null;
  let lastTeamQueries = [];

  // Single match prediction state
  let matchTeams1 = '';
  let matchTeams2 = '';
  let matchProbResult = null;

  // Batch prediction state
  let batchMatches = [
    { teams1: '', teams2: '' }
  ];
  let batchResults = null;

  // Data snapshot state
  let dataFilePath = '';
  let loadUseEnvFromJson = true;
  let dataOpLoading = false;
  let dataOpResult = null;

  // Leaderboard state
  let leaderboardInput = '';
  let leaderboardResults = null;
  let leaderboardTitle = '';
  let leaderboardLoading = false;
  let leaderboardError = null;

  function normalizeApiBase(input) {
    return String(input || '').trim().replace(/\/+$/, '');
  }

  function extractErrorMessage(payload, fallback) {
    if (!payload) {
      return fallback;
    }
    if (typeof payload === 'string') {
      return payload;
    }
    const message = payload.error || payload.detail || payload.message;
    if (typeof message === 'string' && message.trim()) {
      return message.trim();
    }
    return fallback;
  }

  async function apiRequest(path, { method = 'GET', payload } = {}) {
    const options = { method, headers: {} };
    if (payload !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload);
    }

    let response;
    try {
      response = await fetch(`${apiBase}${path}`, options);
    } catch (err) {
      throw new Error(`Network request failed: ${err.message}`);
    }

    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    let responseBody = {};

    if (contentType.includes('application/json')) {
      responseBody = await response.json();
    } else {
      const text = await response.text();
      responseBody = text ? { detail: text } : {};
    }

    if (!response.ok) {
      throw new Error(
        extractErrorMessage(responseBody, `Request failed (${response.status})`)
      );
    }

    return responseBody;
  }

  function getWsBase(httpBase) {
    try {
      const url = new URL(httpBase);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      return normalizeApiBase(url.toString());
    } catch (err) {
      return normalizeApiBase(String(httpBase || '').replace(/^http/i, 'ws'));
    }
  }

  function disconnectWebSocket() {
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    wsStatus = 'disconnected';
  }

  function connectWebSocket() {
    if (typeof window === 'undefined') {
      return;
    }

    disconnectWebSocket();
    wsStatus = 'connecting';
    wsError = null;

    try {
      ws = new WebSocket(`${getWsBase(apiBase)}/ws`);
    } catch (err) {
      wsStatus = 'error';
      wsError = err.message;
      return;
    }

    ws.onopen = () => {
      wsStatus = 'connected';
      wsError = null;
    };

    ws.onmessage = (event) => {
      wsLastUpdate = new Date().toLocaleTimeString();
      try {
        const payload = JSON.parse(event.data);
        if (
          payload &&
          payload.type === 'leaderboard_update' &&
          payload.data &&
          Array.isArray(payload.data.teams)
        ) {
          leaderboardResults = payload.data.teams;
          if (!leaderboardTitle) {
            leaderboardTitle = 'Live Leaderboard';
          }
        }
      } catch (err) {
        // Ignore non-JSON messages.
      }
    };

    ws.onerror = () => {
      wsStatus = 'error';
      wsError = 'Live feed connection failed';
    };

    ws.onclose = () => {
      if (wsStatus !== 'error') {
        wsStatus = 'disconnected';
      }
    };
  }

  async function applyApiBase() {
    const normalized = normalizeApiBase(apiBaseDraft);
    if (!normalized) {
      apiConfigMessage = 'Please enter a valid API URL.';
      return;
    }

    apiBase = normalized;
    apiBaseDraft = normalized;
    apiConfigMessage = `Using API: ${apiBase}`;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(LOCAL_STORAGE_API_KEY, apiBase);
    }

    connectWebSocket();
    await checkHealth();
  }

  async function checkHealth() {
    healthLoading = true;
    healthResult = null;
    healthError = null;
    try {
      healthResult = await apiRequest('/health');
    } catch (err) {
      healthError = err.message;
    } finally {
      healthLoading = false;
    }
  }

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

  function parseTeamsInput(raw) {
    return String(raw || '')
      .split(',')
      .map((team) => team.trim().toLowerCase())
      .filter(Boolean);
  }

  async function doUpdate() {
    updateResult = null;
    updateInProgress = true;

    let payload = null;
    if (updateMode === 'event') {
      const trimmed = eventKey.trim();
      if (!trimmed) {
        alert('Please enter an event key.');
        updateInProgress = false;
        return;
      }
      payload = { event_key: trimmed };
    } else {
      const parsedYear = Number(year);
      if (!Number.isInteger(parsedYear)) {
        alert('Please enter a valid year.');
        updateInProgress = false;
        return;
      }
      payload = { year: parsedYear };
    }

    try {
      updateResult = await apiRequest('/update', { method: 'POST', payload });
      if (leaderboardResults) {
        await fetchLeaderboardOnly();
      }
      await checkHealth();
    } catch (err) {
      updateResult = { error: err.message };
    } finally {
      updateInProgress = false;
    }
  }

  async function doPush() {
    pushResponse = null;

    const matchesPayload = pushMatches
      .map((m) => ({
        teams1: parseTeamsInput(m.teams1),
        teams2: parseTeamsInput(m.teams2),
        score1: Number(m.score1),
        score2: Number(m.score2)
      }))
      .filter((m) => (
        m.teams1.length > 0 &&
        m.teams2.length > 0 &&
        Number.isFinite(m.score1) &&
        Number.isFinite(m.score2)
      ));

    if (!matchesPayload.length) {
      pushResponse = { error: 'Add at least one valid match before submitting.' };
      return;
    }

    try {
      pushResponse = await apiRequest('/push_results', {
        method: 'POST',
        payload: matchesPayload
      });
      if (leaderboardResults) {
        await fetchLeaderboardOnly();
      }
    } catch (err) {
      pushResponse = { error: err.message };
    }
  }

  async function doQueryTeam() {
    teamResult = null;
    if (!teamQuery.trim()) {
      alert('Please enter a team key (e.g. frc254).');
      return;
    }

    try {
      const team = teamQuery.trim().toLowerCase();
      teamResult = await apiRequest(`/predict_team?team=${encodeURIComponent(team)}`);
      lastTeamQueries = [teamResult, ...lastTeamQueries].slice(0, 5);
    } catch (err) {
      teamResult = { error: err.message };
    }
  }

  async function doPredictMatch() {
    matchProbResult = null;
    const teams1 = parseTeamsInput(matchTeams1);
    const teams2 = parseTeamsInput(matchTeams2);
    if (!teams1.length || !teams2.length) {
      alert('Please enter team lists for both alliances.');
      return;
    }

    try {
      matchProbResult = await apiRequest('/predict_match', {
        method: 'POST',
        payload: { teams1, teams2 }
      });
    } catch (err) {
      matchProbResult = { error: err.message };
    }
  }

  async function doBatchPredict() {
    batchResults = null;
    const batchPayload = batchMatches.map((m) => ({
      teams1: parseTeamsInput(m.teams1),
      teams2: parseTeamsInput(m.teams2)
    }));

    try {
      const data = await apiRequest('/predict_batch', {
        method: 'POST',
        payload: batchPayload
      });
      batchResults = Array.isArray(data) ? data : [{ error: 'Unexpected response format' }];
    } catch (err) {
      batchResults = [{ error: err.message }];
    }
  }

  async function fetchLeaderboardOnly() {
    leaderboardError = null;
    const data = await apiRequest('/leaderboard');
    leaderboardResults = Array.isArray(data.teams) ? data.teams : [];
  }

  async function doLeaderboard() {
    leaderboardResults = null;
    leaderboardError = null;

    const input = leaderboardInput.trim();
    if (!input) {
      alert('Please enter a year or event key.');
      return;
    }

    const isYear = /^\d{4}$/.test(input);
    const payload = isYear ? { year: Number(input) } : { event_key: input.toLowerCase() };
    leaderboardTitle = isYear ? `Year ${input}` : `Event ${input}`;

    try {
      leaderboardLoading = true;
      await apiRequest('/update', { method: 'POST', payload });
      await fetchLeaderboardOnly();
      await checkHealth();
    } catch (err) {
      leaderboardError = err.message;
    } finally {
      leaderboardLoading = false;
    }
  }

  async function doUploadData() {
    dataOpResult = null;
    dataOpLoading = true;
    try {
      dataOpResult = await apiRequest('/upload_data', { method: 'POST' });
    } catch (err) {
      dataOpResult = { error: err.message };
    } finally {
      dataOpLoading = false;
    }
  }

  async function doLoadData() {
    dataOpResult = null;
    dataOpLoading = true;

    const payload = { use_env_from_json: loadUseEnvFromJson };
    if (dataFilePath.trim()) {
      payload.path = dataFilePath.trim();
    }

    try {
      dataOpResult = await apiRequest('/load_data', {
        method: 'POST',
        payload
      });
      if (leaderboardResults) {
        await fetchLeaderboardOnly();
      }
      await checkHealth();
    } catch (err) {
      dataOpResult = { error: err.message };
    } finally {
      dataOpLoading = false;
    }
  }

  async function doRecalculate(source) {
    dataOpResult = null;
    dataOpLoading = true;

    const payload = source === 'json' ? { source: 'json' } : {};
    try {
      dataOpResult = await apiRequest('/recalculate', {
        method: 'POST',
        payload
      });
      if (leaderboardResults) {
        await fetchLeaderboardOnly();
      }
      await checkHealth();
    } catch (err) {
      dataOpResult = { error: err.message };
    } finally {
      dataOpLoading = false;
    }
  }

  onMount(() => {
    if (typeof window !== 'undefined') {
      const savedBase = normalizeApiBase(window.localStorage.getItem(LOCAL_STORAGE_API_KEY));
      if (savedBase) {
        apiBase = savedBase;
        apiBaseDraft = savedBase;
      }
    }

    apiConfigMessage = `Using API: ${apiBase}`;
    void checkHealth();
    connectWebSocket();
  });

  onDestroy(() => {
    disconnectWebSocket();
  });
</script>

<style>
  :global(body) {
    font-family: Arial, sans-serif;
    background: #f0f0f0;
    margin: 1rem;
  }

  .section {
    background: #fff;
    border: none;
    border-radius: 5px;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
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

  button:disabled {
    background-color: #9cbad0;
    cursor: not-allowed;
  }

  button[type='button'] {
    margin-left: 0;
  }

  .section fieldset {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
  }

  .section fieldset input {
    margin-right: 0.5rem;
  }

  .section fieldset button {
    margin-top: 0.3rem;
  }

  table.leaderboard-table {
    width: 100%;
    border-collapse: collapse;
  }

  table.leaderboard-table th,
  table.leaderboard-table td {
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

  .loading {
    font-style: italic;
    color: #555;
  }

  .status-badge {
    display: inline-block;
    padding: 0.15rem 0.45rem;
    border-radius: 0.3rem;
    text-transform: capitalize;
    font-size: 0.85rem;
    font-weight: bold;
    color: white;
    margin-left: 0.3rem;
  }

  .status-badge.connected {
    background: #198754;
  }

  .status-badge.connecting {
    background: #0d6efd;
  }

  .status-badge.disconnected,
  .status-badge.error {
    background: #c0392b;
  }

  .inline-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    margin-right: 0.75rem;
  }
</style>

<div class="section">
  <h2>API Connection</h2>
  <input
    type="text"
    placeholder="API URL (e.g. http://127.0.0.1:5000)"
    bind:value={apiBaseDraft}
  >
  <button on:click={applyApiBase}>Apply API URL</button>
  <button on:click={checkHealth} disabled={healthLoading}>Check Health</button>
  <button on:click={connectWebSocket}>Reconnect Live Feed</button>
  <p>{apiConfigMessage}</p>

  {#if healthLoading}
    <p class="loading">Checking backend health...</p>
  {/if}
  {#if healthError}
    <p class="error">Health check failed: {healthError}</p>
  {/if}
  {#if healthResult}
    <p>
      API healthy: {healthResult.ok ? 'yes' : 'no'} |
      DB connected: {healthResult.db_connected ? 'yes' : 'no'} |
      Teams indexed: {healthResult.teams_indexed}
    </p>
  {/if}

  <p>
    Live updates:
    <span class={`status-badge ${wsStatus}`}>{wsStatus}</span>
  </p>
  {#if wsLastUpdate}
    <p>Last live message: {wsLastUpdate}</p>
  {/if}
  {#if wsError}
    <p class="error">Live feed error: {wsError}</p>
  {/if}
</div>

<div class="section">
  <h2>Update Ratings</h2>
  <div>
    <label class="inline-label">
      <input type="radio" bind:group={updateMode} value="event">
      Update by Event
    </label>
    <label class="inline-label">
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
        {#if updateResult.teams_indexed !== undefined}
          (Teams indexed: {updateResult.teams_indexed})
        {/if}
      </p>
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Push Match Results</h2>
  {#each pushMatches as match, i}
    <fieldset>
      <legend>Match {i + 1}</legend>
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
      <p>
        Team {String(teamResult.team || '').toUpperCase()}:
        mu = {Number(teamResult.mu).toFixed(2)},
        sigma = {Number(teamResult.sigma).toFixed(2)}
      </p>
      <p>Conservative rating (mu-3sigma): {Number(teamResult.conservative_mu_3sigma).toFixed(2)}</p>
      <p>Rating confidence: {Number(teamResult.confidence_percent).toFixed(2)}%</p>
    {/if}
  {/if}
  {#if lastTeamQueries.length > 0}
    <h3>Recent Queries</h3>
    <ul>
      {#each lastTeamQueries as q}
        <li>
          Team {String(q.team || '').toUpperCase()}:
          mu = {Number(q.mu).toFixed(2)},
          sigma = {Number(q.sigma).toFixed(2)},
          mu-3sigma = {Number(q.conservative_mu_3sigma).toFixed(2)},
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
      <p>Alliance 1 Win Probability: {(Number(matchProbResult.team1_win_prob) * 100).toFixed(2)}%</p>
      <p>Alliance 2 Win Probability: {(Number(matchProbResult.team2_win_prob) * 100).toFixed(2)}%</p>
      {#if matchProbResult.prediction_confidence_percent !== undefined}
        <p>Prediction confidence: {Number(matchProbResult.prediction_confidence_percent).toFixed(2)}%</p>
      {/if}
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Batch Match Predictions</h2>
  {#each batchMatches as match, j}
    <fieldset>
      <legend>Match {j + 1}</legend>
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
        <p class="error">Match {k + 1}: Error: {result.error}</p>
      {:else}
        <p>
          Match {k + 1}:
          Alliance 1 Win = {(Number(result.team1_win_prob) * 100).toFixed(2)}%,
          Alliance 2 Win = {(Number(result.team2_win_prob) * 100).toFixed(2)}%
        </p>
      {/if}
    {/each}
  {/if}
</div>

<div class="section">
  <h2>Data Snapshot</h2>
  <input
    type="text"
    placeholder="Optional JSON path (uses API default if empty)"
    bind:value={dataFilePath}
  >
  <label class="inline-label">
    <input type="checkbox" bind:checked={loadUseEnvFromJson}>
    Use env values from JSON on load
  </label>
  <div>
    <button on:click={doUploadData} disabled={dataOpLoading}>Upload Data</button>
    <button on:click={doLoadData} disabled={dataOpLoading}>Load Data</button>
    <button on:click={() => doRecalculate('memory')} disabled={dataOpLoading}>
      Recalculate (Memory)
    </button>
    <button on:click={() => doRecalculate('json')} disabled={dataOpLoading}>
      Recalculate (Reload JSON)
    </button>
  </div>
  {#if dataOpLoading}
    <p class="loading">Running data operation...</p>
  {/if}
  {#if dataOpResult}
    {#if dataOpResult.error}
      <p class="error">Error: {dataOpResult.error}</p>
    {:else}
      <p>Status: {dataOpResult.status}</p>
      {#if dataOpResult.file}
        <p>File: {dataOpResult.file}</p>
      {/if}
      {#if dataOpResult.teams_indexed !== undefined}
        <p>Teams indexed: {dataOpResult.teams_indexed}</p>
      {/if}
      {#if dataOpResult.saved_teams_indexed !== undefined}
        <p>Saved teams indexed: {dataOpResult.saved_teams_indexed}</p>
      {/if}
    {/if}
  {/if}
</div>

<div class="section">
  <h2>Leaderboard</h2>
  <input
    type="text"
    placeholder="Year or Event Key (e.g. 2025 or 2025miket)"
    bind:value={leaderboardInput}
  >
  <button on:click={doLeaderboard} disabled={leaderboardLoading}>Generate Leaderboard</button>
  <button on:click={fetchLeaderboardOnly} disabled={leaderboardLoading}>Refresh Current Leaderboard</button>
  {#if leaderboardLoading}
    <p class="loading">Calculating rankings, please wait...</p>
  {/if}
  {#if leaderboardError}
    <p class="error">Error: {leaderboardError}</p>
  {/if}
  {#if leaderboardResults}
    <p><strong>Leaderboard for {leaderboardTitle || 'Current Memory State'}</strong> - Total Teams: {leaderboardResults.length}</p>
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Team</th>
          <th>mu</th>
          <th>sigma</th>
          <th>mu-3sigma</th>
          <th>Conf%</th>
        </tr>
      </thead>
      <tbody>
        {#each leaderboardResults as entry}
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
