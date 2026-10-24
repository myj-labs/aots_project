const $ = (id) => document.getElementById(id);
const inr = (n) => new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2}).format(Number(n||0));
document.querySelectorAll(".app-tab").forEach(button => {
  button.addEventListener("click", () => {
    const selected = button.dataset.tab;
    document.querySelectorAll(".app-tab").forEach(tab => {
      tab.setAttribute("aria-pressed", String(tab === button));
    });
    document.querySelectorAll("[data-app-tab]").forEach(panel => {
      panel.hidden = panel.dataset.appTab !== selected;
    });
  });
});
let pendingEntryKey = null;
let entryRequestActive = false;
async function api(path, options={}) {
  const res = await fetch(path,{headers:{"Content-Type":"application/json"},...options});
  const body = await res.json().catch(()=>({detail:"Unexpected response"}));
  if(!res.ok) {
    const err = new Error(errorText(body.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}
function errorText(detail) {
  if(Array.isArray(detail)) {
    return detail.map(d => {
      const field = Array.isArray(d.loc) ? d.loc.filter(p => p !== "body").join(".") : "";
      return field ? `${field}: ${d.msg}` : String(d.msg || "Invalid request");
    }).join("; ");
  }
  if(detail && typeof detail === "object") return String(detail.message || detail.msg || "Request failed");
  return detail;
}
let latestRefresh = 0;
async function refresh() {
  // Only the most recently started refresh may render; older responses are discarded.
  const generation = ++latestRefresh;
  const isCurrent = () => generation === latestRefresh;
  // Optional strategy history is fetched independently so its failure cannot block core sections.
  const decisionsResult = api("/api/strategy/decisions").then(value => ({value}), error => ({error}));
  try {
    const [status,risk,trades] = await Promise.all([api("/api/status"),api("/api/risk"),api("/api/trades")]);
    if(!isCurrent()) return;
    $("health-label").textContent = "System healthy · paper only";
    $("capital").textContent = inr(status.capital_inr);
    $("strategy-threshold").textContent = status.strategy_spot_threshold == null ?
      "Not configured" : `${Number(status.strategy_spot_threshold).toLocaleString("en-IN")} (${status.underlying})`;
    $("pnl").textContent = inr(risk.daily_pnl);
    $("pnl").className = risk.daily_pnl >= 0 ? "positive" : "negative";
    $("realised-pnl").textContent = inr(risk.realised_pnl);
    $("realised-pnl").className = risk.realised_pnl >= 0 ? "positive" : "negative";
    $("unrealised-pnl").textContent = inr(risk.unrealised_pnl);
    $("unrealised-pnl").className = risk.unrealised_pnl >= 0 ? "positive" : "negative";
    $("positions-count").textContent = risk.open_positions;
    $("loss-limit").textContent = inr(risk.max_daily_loss_inr);
    $("trade-risk").textContent = `${(risk.max_trade_risk_pct*100).toFixed(2)}% · ${inr(risk.max_trade_risk_inr)}`;
    $("loss-streak").textContent = `${risk.consecutive_losses} / ${risk.max_consecutive_losses}`;
    $("pause-status").textContent = risk.paused_for_day ? "Paused for today" : "Not paused";
    $("daily-pnl").textContent = inr(risk.daily_pnl);
    $("kill-status").textContent = risk.kill_switch ? "ON" : "OFF";
    $("risk-badge").textContent = risk.kill_switch ? "Entries blocked" : risk.paused_for_day ? "Paused" : "Active";
    $("risk-badge").style.background = risk.kill_switch || risk.paused_for_day ? "#49242c" : "#183b35";
    $("risk-badge").style.color = risk.kill_switch || risk.paused_for_day ? "#ffb7bd" : "#81e2c0";
    $("kill-toggle").textContent = risk.kill_switch ? "Disable kill switch" : "Enable kill switch";
    $("kill-toggle").className = risk.kill_switch ? "" : "danger";
    renderTrades(trades);
  } catch(e) {
    if(!isCurrent()) return;
    $("health-label").textContent = `API unavailable: ${e.message}`;
    $("risk-badge").textContent = "Data unavailable";
    $("trades-body").innerHTML = '<tr><td colspan="15" class="negative">Trades unavailable: core data could not be refreshed. Displayed values may be out of date.</td></tr>';
  }
  const decisions = await decisionsResult;
  if(!isCurrent()) return;
  if(decisions.error) {
    $("strategy-decisions").innerHTML = `<tr><td colspan="5" class="negative">Strategy history unavailable: ${escapeHtml(decisions.error.message)}</td></tr>`;
  } else {
    renderStrategyDecisions(decisions.value);
  }
}
function renderStrategyDecisions(decisions) {
  const body = $("strategy-decisions");
  if(!decisions.length){body.innerHTML='<tr><td colspan="5" class="muted">No processed observations yet. Evaluation-only results are not persisted.</td></tr>';return;}
  body.innerHTML = decisions.map(d => {
    const candidate = d.resulting_trade_id ? `Trade ${escapeHtml(d.resulting_trade_id)}` :
      d.signal ? `${escapeHtml(d.signal.symbol)} · BUY` : "—";
    return `<tr><td>${escapeHtml(d.evaluated_at)}</td><td>${escapeHtml(d.observation_id)}</td><td>${escapeHtml(d.status)}</td><td>${escapeHtml(d.reason)}</td><td>${candidate}</td></tr>`;
  }).join("");
}
function renderTrades(trades) {
  const body = $("trades-body");
  if(!trades.length){body.innerHTML='<tr><td colspan="15" class="muted">No paper trades yet. Create a simulated trade above to test the workflow.</td></tr>';return;}
  body.innerHTML = trades.map(t => {
    const pnl = t.status==="OPEN" ? Number(t.unrealised_pnl) : Number(t.realised_pnl);
    const pnlClass = pnl >= 0 ? "positive" : "negative";
    const status = t.close_reason==="SIMULATED_TAKE_PROFIT" ? "CLOSED · Take-profit reached" :
      t.close_reason==="SIMULATED_STOP" ? "CLOSED · Stop-loss triggered" : t.status;
    const actions = t.status==="OPEN" ?
      `<button data-action="mark" data-id="${escapeHtml(t.trade_id)}" data-sequence="${Number(t.mark_sequence)+1}">Mark</button><button data-action="close" data-id="${escapeHtml(t.trade_id)}">Close</button>` : "—";
    const target = t.take_profit_profit_inr == null ? "—" : inr(t.take_profit_profit_inr);
    const mark = t.status==="CLOSED" || Number(t.mark_sequence)>0 ? inr(t.mark_price) : "No mark submitted";
    const pnlType = t.status==="OPEN" ? "Open unrealised" : "Closed realised";
    const closeReason = {
      SIMULATED_TAKE_PROFIT:"Take-profit reached",
      SIMULATED_STOP:"Stop-loss triggered",
      MANUAL:"Manual close",
    }[t.close_reason] || t.close_reason || "—";
    const side = t.side==="BUY" ? "BUY · Long" : t.side;
    return `<tr><td>${escapeHtml(t.symbol)}</td><td>${escapeHtml(side)}</td><td>Not recorded</td><td>${escapeHtml(status)}</td><td>${inr(t.entry_price)}</td><td>${inr(t.stop_price)}</td><td>${target}</td><td>${mark}</td><td>${Number(t.quantity)}</td><td>${Number(t.lots)}</td><td class="${pnlClass}">${pnlType}<br>${inr(pnl)}</td><td>${kolkataTime(t.created_at)}</td><td>${kolkataTime(t.closed_at)}</td><td>${escapeHtml(closeReason)}</td><td>${actions}</td></tr>`;
  }).join("");
}
function kolkataTime(value){
  if(!value)return "—";
  const timestamp=new Date(value);
  if(Number.isNaN(timestamp.getTime()))return "Unavailable";
  return `${new Intl.DateTimeFormat("en-IN",{dateStyle:"medium",timeStyle:"short",timeZone:"Asia/Kolkata"}).format(timestamp)} IST`;
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
$("refresh").addEventListener("click",refresh);
$("kill-toggle").addEventListener("click",async()=>{
  try { const r=await api("/api/risk"); await api("/api/risk/kill-switch",{method:"POST",body:JSON.stringify({enabled:!r.kill_switch})}); await refresh(); }
  catch(e){alert(e.message);}
});
$("trade-form").addEventListener("submit",async(e)=>{
  e.preventDefault(); if(entryRequestActive)return;
  const form=e.currentTarget; const button=form.querySelector('button[type="submit"]');
  const f=new FormData(form); const data=Object.fromEntries(f.entries());
  ["entry_price","stop_price","quantity","lots","market_data_age_seconds"].forEach(k=>data[k]=Number(data[k]));
  const target = data.take_profit_profit_inr.trim();
  data.take_profit_profit_inr = target ? Number(target) : null;
  if(data.take_profit_profit_inr !== null && (!Number.isFinite(data.take_profit_profit_inr) || data.take_profit_profit_inr <= 0)){
    $("form-message").textContent="Take-profit profit target must be a positive rupee amount.";
    $("form-message").className="negative";
    return;
  }
  pendingEntryKey ||= crypto.randomUUID();
  data.idempotency_key=pendingEntryKey;
  entryRequestActive=true; button.disabled=true;
  try { await api("/api/paper/trade",{method:"POST",body:JSON.stringify(data)}); pendingEntryKey=null; $("form-message").textContent="Simulated trade created."; $("form-message").className="positive"; await refresh(); }
  catch(err){$("form-message").textContent=err.message;$("form-message").className="negative";}
  finally {entryRequestActive=false;button.disabled=false;}
});
$("trade-form").addEventListener("input",()=>{if(!entryRequestActive)pendingEntryKey=null;});
$("strategy-evaluate").addEventListener("click",()=>submitStrategy("/api/strategy/evaluate"));
$("strategy-process").addEventListener("click",()=>submitStrategy("/api/strategy/process"));
async function submitStrategy(path) {
  const form = $("strategy-form");
  const values = Object.fromEntries(new FormData(form).entries());
  values.spot = Number(values.spot);
  values.entry_price = Number(values.entry_price);
  values.stop_price = Number(values.stop_price);
  values.quantity = Number(values.quantity);
  values.lots = Number(values.lots);
  values.take_profit_profit_inr = values.take_profit_profit_inr.trim() ?
    Number(values.take_profit_profit_inr) : null;
  $("strategy-message").textContent = "Submitting supplied inputs…";
  $("strategy-message").className = "muted";
  try {
    const decision = await api(path,{method:"POST",body:JSON.stringify(values)});
    $("strategy-message").textContent = `${decision.status}: ${decision.reason}`;
    $("strategy-message").className = decision.status==="TRADE_CREATED" ? "positive" :
      decision.status==="SIGNAL" || decision.status==="NO_SIGNAL" ? "muted" : "negative";
    if(path==="/api/strategy/process")await refresh();
  } catch(err) {
    $("strategy-message").textContent = err.message;
    $("strategy-message").className = "negative";
  }
}
$("trades-body").addEventListener("click",async(e)=>{
  const b=e.target.closest("button"); if(!b)return;
  try{
    if(b.dataset.action==="mark"){
      const raw=prompt("Enter simulated option mark price (₹):","98"); if(raw===null)return;
      await api("/api/paper/mark",{method:"POST",body:JSON.stringify({trade_id:b.dataset.id,mark_price:Number(raw),observation_sequence:Number(b.dataset.sequence)})});
    } else {
      const raw=prompt("Enter simulated exit price (₹):","98"); if(raw===null)return;
      await api(`/api/paper/close/${b.dataset.id}?exit_price=${encodeURIComponent(Number(raw))}`,{method:"POST"});
    }
    await refresh();
  }catch(err){await refresh();alert(err.message);}
});
refresh();

// Market-data provider panel. Credentials are only sent in the connect request body and are
// cleared from the inputs immediately; nothing is stored in browser storage, URLs or cookies.
const PROVIDER_LABELS = {
  not_configured:"Not configured", credentials_in_memory:"Credentials in memory; live connection not implemented",
  connecting:"Connecting", connected:"Connected", failed:"Connection failed", disconnected:"Disconnected"
};
function clearProviderInputs() {
  document.querySelectorAll("#provider-form input").forEach(i => { i.value = ""; });
}
function renderProviderStatus(s) {
  $("provider-status").textContent = PROVIDER_LABELS[s.state] || "Unknown";
  $("provider-status").dataset.state = s.state;
  $("account-connection-state").textContent = PROVIDER_LABELS[s.state] || "Unknown";
  const detail = s.failure_reason ? ` ${s.failure_reason}` : "";
  $("provider-message").textContent = (PROVIDER_LABELS[s.state] || "") + "." + detail;
  $("provider-message").className = s.state === "failed" ? "negative" : "muted";
}
function syncProviderFields() {
  const method = $("provider-auth-method").value;
  document.querySelectorAll("#provider-form [data-auth]").forEach(l => {
    const show = l.dataset.auth === method;
    l.hidden = !show;
    if(!show) l.querySelector("input").value = "";
  });
}
async function loadProviderStatus() {
  try { renderProviderStatus(await api("/api/provider/status")); }
  catch(err) { $("provider-message").textContent = "Provider status unavailable."; $("provider-message").className = "negative"; }
}
$("provider-auth-method").addEventListener("change", syncProviderFields);
$("provider-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  new FormData(e.target).forEach((v, k) => { if(v !== "") body[k] = v; });
  const payload = JSON.stringify(body);
  clearProviderInputs();
  $("provider-message").textContent = "Connecting…"; $("provider-message").className = "muted";
  try { renderProviderStatus(await api("/api/provider/connect", {method:"POST", body:payload})); }
  catch(err) { $("provider-message").textContent = err.message; $("provider-message").className = "negative"; }
});
$("provider-disconnect").addEventListener("click", async () => {
  clearProviderInputs();
  try { renderProviderStatus(await api("/api/provider/disconnect", {method:"POST"})); }
  catch(err) { $("provider-message").textContent = err.message; $("provider-message").className = "negative"; }
});
loadProviderStatus();

let accountRefreshGeneration = 0;
let accountRefreshInProgress = false;
let lastAccountRefreshAt = null;

const optionalMoney = value => value == null ? "Unavailable" : inr(value);
function setAccountRows(id, colspan, section, renderRow, emptyText) {
  const body = $(id);
  if(section.status === "unavailable" || !Array.isArray(section.items)) {
    body.innerHTML = `<tr><td colspan="${colspan}" class="negative">${escapeHtml(section.reason || "Data unavailable.")}</td></tr>`;
    return;
  }
  if(!section.items.length) {
    body.innerHTML = `<tr><td colspan="${colspan}" class="muted">${emptyText}</td></tr>`;
    return;
  }
  body.innerHTML = section.items.map(renderRow).join("");
}
function renderAccountInsights(data) {
  const positions = data.positions || {status:"unavailable",reason:"Positions unavailable.",items:null};
  const openPositions = Array.isArray(positions.items) ?
    positions.items.filter(row => row.quantity != null && row.quantity !== 0) : null;
  const visiblePositions = openPositions === null ? positions :
    {...positions, items:openPositions};
  setAccountRows("groww-positions", 7, visiblePositions, row =>
    `<tr><td>${escapeHtml(row.trading_symbol || "Unavailable")}</td><td>${escapeHtml(row.segment || "Unavailable")}</td><td>${escapeHtml(row.exchange || "Unavailable")}</td><td>${escapeHtml(row.product || "Unavailable")}</td><td>${row.quantity == null ? "Unavailable" : Number(row.quantity)}</td><td>${optionalMoney(row.net_price)}</td><td>${optionalMoney(row.reported_realised_pnl)}</td></tr>`,
    "No open Groww positions.");
  setAccountRows("groww-holdings", 5, data.holdings || {}, row =>
    `<tr><td>${escapeHtml(row.trading_symbol || "Unavailable")}</td><td>${row.quantity == null ? "Unavailable" : Number(row.quantity)}</td><td>${optionalMoney(row.average_price)}</td><td>${optionalMoney(row.pledge_quantity)}</td><td>${optionalMoney(row.t1_quantity)}</td></tr>`,
    "No Groww holdings.");

  const balances = data.balances || {};
  const balanceValues = balances.values || {};
  $("groww-clear-cash").textContent = balances.status === "available" ?
    optionalMoney(balanceValues.clear_cash) : "Unavailable";
  $("groww-collateral").textContent = balances.status === "available" ?
    optionalMoney(balanceValues.collateral_available) : "Unavailable";
  const todayPnl = data.today_pnl || {};
  $("groww-today-pnl").textContent = todayPnl.status === "available" ?
    optionalMoney(todayPnl.value) : "Unavailable";
  $("groww-today-pnl-reason").textContent = todayPnl.reason ||
    (todayPnl.status === "available" ? "Groww-reported value." : "Today's P&L is unavailable.");

  const sections = [
    ["positions", positions],
    ["holdings", data.holdings || {}],
    ["balances", balances],
  ];
  const unavailableCount = sections.filter(([, section]) => section.status !== "available").length;
  const partial = unavailableCount > 0;
  $("account-insights-state").textContent = partial ? "Partially available" : "Refreshed";
  $("account-insights-state").dataset.state = partial ? "partial" : "available";
  const reasons = sections
    .filter(([, section]) => section.status !== "available")
    .map(([name, section]) => `${name}: ${section.reason || "unavailable"}`);
  $("account-insights-message").textContent = reasons.length ?
    `Some account data is unavailable: ${reasons.join(" ")}` :
    "Groww account data refreshed. This is not AOTS paper-trade data.";
  $("account-insights-message").className = partial ? "negative" : "muted";
  lastAccountRefreshAt = data.refreshed_at || null;
  $("account-refresh-time").textContent = lastAccountRefreshAt ?
    `Last successful refresh: ${kolkataTime(lastAccountRefreshAt)}` :
    "Last successful refresh: timestamp unavailable";
}

async function refreshAccountInsights() {
  if(accountRefreshInProgress) return;
  accountRefreshInProgress = true;
  const generation = ++accountRefreshGeneration;
  const button = $("account-insights-refresh");
  button.disabled = true;
  $("account-insights-state").textContent = "Loading";
  $("account-insights-state").dataset.state = "loading";
  $("account-insights-message").textContent = "Loading Groww account data…";
  $("account-insights-message").className = "muted";
  try {
    const data = await api("/api/provider/account-insights");
    if(generation !== accountRefreshGeneration) return;
    renderAccountInsights(data);
  } catch(err) {
    if(generation !== accountRefreshGeneration) return;
    $("account-insights-state").textContent = err.status === 401 ?
      "Session expired" : "Refresh failed";
    $("account-insights-state").dataset.state = err.status === 401 ?
      "expired" : "error";
    const priorTime = lastAccountRefreshAt ?
      ` Last successful data is from ${kolkataTime(lastAccountRefreshAt)} and is stale.` :
      " No account snapshot is available.";
    $("account-insights-message").textContent = `${err.message}.${priorTime}`;
    $("account-insights-message").className = "negative";
    if(err.status === 401) loadProviderStatus();
  } finally {
    if(generation === accountRefreshGeneration) {
      accountRefreshInProgress = false;
      button.disabled = false;
    }
  }
}
$("account-insights-refresh").addEventListener("click", refreshAccountInsights);

const historicalMonthSelect = $("historical-expiry-form").elements.month;
for(let month = 1; month <= 12; month++) {
  const option = document.createElement("option");
  option.value = String(month);
  option.textContent = new Intl.DateTimeFormat("en-IN", {month:"long"}).format(new Date(2024, month - 1, 1));
  historicalMonthSelect.appendChild(option);
}
historicalMonthSelect.value = String(new Date().getMonth() + 1);
let historicalRequestGeneration = 0;
let historicalRequestActive = false;
function updateHistoricalControls() {
  $("historical-expiry-load").disabled = historicalRequestActive;
  $("historical-contracts-load").disabled =
    historicalRequestActive || !$("historical-expiry").value;
  $("historical-candles-check").disabled =
    historicalRequestActive || !$("historical-contract").value;
}
function historicalStatus(state, message) {
  const labels = {
    checking:"Checking",
    available:"Data returned",
    empty:"No data",
    denied:"Access denied",
    unauthorized:"Reconnect required",
    failed:"Check failed",
  };
  $("historical-check-status").textContent = labels[state];
  $("historical-check-status").dataset.state = state;
  $("historical-check-message").textContent = message;
  $("historical-check-message").className =
    state === "available" ? "positive" :
    ["denied", "unauthorized", "failed"].includes(state) ? "negative" : "muted";
}
function historicalFailure(error) {
  const state = error.status === 403 ? "denied" :
    error.status === 401 ? "unauthorized" : "failed";
  const message = error.status === 403 ?
    "Groww denied access. The specific cause may require verification." : error.message;
  historicalStatus(state, message);
  if(state === "unauthorized") loadProviderStatus();
}
function resetHistoricalContracts() {
  const expiry = $("historical-expiry");
  expiry.innerHTML = '<option value="">Load expiries first</option>';
  expiry.disabled = true;
  $("historical-contracts-load").disabled = true;
  resetHistoricalContractOptions();
}
function resetHistoricalContractOptions() {
  const contract = $("historical-contract");
  contract.innerHTML = '<option value="">Load contracts first</option>';
  contract.disabled = true;
  $("historical-candles-check").disabled = true;
}
function updateHistoricalContractSelection() {
  $("historical-candles-check").disabled = !$("historical-contract").value;
}
$("historical-expiry-form").addEventListener("submit", async event => {
  event.preventDefault();
  if(historicalRequestActive) return;
  const generation = ++historicalRequestGeneration;
  const form = event.currentTarget;
  const query = new URLSearchParams({
    year: form.elements.year.value,
    month: form.elements.month.value,
  });
  historicalRequestActive = true;
  updateHistoricalControls();
  resetHistoricalContracts();
  $("historical-check-result").hidden = true;
  historicalStatus("checking", "Requesting NIFTY historical expiries from Groww…");
  try {
    const response = await api(`/api/provider/historical/expiries?${query}`);
    if(generation !== historicalRequestGeneration) return;
    const expiry = $("historical-expiry");
    expiry.replaceChildren(new Option("Choose an expiry", ""));
    for(const item of response.expiries) expiry.add(new Option(item, item));
    expiry.disabled = response.expiries.length === 0;
    $("historical-contracts-load").disabled = response.expiries.length === 0;
    updateHistoricalControls();
    historicalStatus(response.expiries.length ? "available" : "empty", response.expiries.length ?
      `${response.expiries.length} expiry date(s) returned. Select one, then load its contracts.` :
      "Request succeeded but no expiries were returned for that month.");
  } catch(error) {
    if(generation !== historicalRequestGeneration) return;
    historicalFailure(error);
  } finally {
    historicalRequestActive = false;
    updateHistoricalControls();
  }
});
$("historical-expiry").addEventListener("change", () => {
  resetHistoricalContractOptions();
  $("historical-contracts-load").disabled = !$("historical-expiry").value;
});
$("historical-contracts-load").addEventListener("click", async () => {
  if(historicalRequestActive) return;
  const generation = ++historicalRequestGeneration;
  const expiry = $("historical-expiry").value;
  if(!expiry) return;
  historicalRequestActive = true;
  updateHistoricalControls();
  resetHistoricalContractOptions();
  historicalStatus("checking", "Requesting contracts for the selected expiry from Groww…");
  try {
    const query = new URLSearchParams({expiry_date:expiry});
    const response = await api(`/api/provider/historical/contracts?${query}`);
    if(generation !== historicalRequestGeneration) return;
    const contract = $("historical-contract");
    contract.replaceChildren(new Option("Choose a NIFTY option contract", ""));
    for(const item of response.contracts) contract.add(new Option(item, item));
    contract.disabled = response.contracts.length === 0;
    updateHistoricalContractSelection();
    updateHistoricalControls();
    historicalStatus(response.contracts.length ? "available" : "empty", response.contracts.length ?
      `${response.contracts.length} NIFTY option contract(s) returned. Select one and test candles.` :
      "Request succeeded but no option contracts were returned for that expiry.");
  } catch(error) {
    if(generation !== historicalRequestGeneration) return;
    historicalFailure(error);
  } finally {
    historicalRequestActive = false;
    updateHistoricalControls();
  }
});
$("historical-contract").addEventListener("change", updateHistoricalContractSelection);
$("historical-candles-form").addEventListener("submit", async event => {
  event.preventDefault();
  const contract = $("historical-contract").value;
  if(!contract || historicalRequestActive) return;
  const generation = ++historicalRequestGeneration;
  const form = event.currentTarget;
  historicalRequestActive = true;
  updateHistoricalControls();
  $("historical-check-result").hidden = true;
  historicalStatus("checking", "Checking historical FNO candle access…");
  try {
    const result = await api("/api/provider/historical/candles/check", {
      method:"POST",
      body:JSON.stringify({
        groww_symbol:contract,
        start_time:form.elements.start_time.value,
        end_time:form.elements.end_time.value,
        candle_interval:form.elements.candle_interval.value,
      }),
    });
    if(generation !== historicalRequestGeneration) return;
    $("historical-candle-count").textContent = Number(result.candle_count).toLocaleString("en-IN");
    $("historical-first-timestamp").textContent = result.first_timestamp || "—";
    $("historical-last-timestamp").textContent = result.last_timestamp || "—";
    $("historical-invalid-count").textContent = Number(result.invalid_ohlc_count).toLocaleString("en-IN");
    $("historical-quality-warnings").textContent = [
      ...(result.quality_warnings || []),
      result.timestamps_timezone,
    ].filter(Boolean).join(" ");
    $("historical-check-result").hidden = false;
    const state = result.status === "available" ? "available" : "empty";
    const message = result.status === "available" ?
      "Groww returned historical candles for this contract and window. This is a data-access diagnostic, not a backtest." :
      "The request succeeded but Groww returned no candles. Availability for other contracts or dates is not established.";
    historicalStatus(state, message);
  } catch(error) {
    if(generation !== historicalRequestGeneration) return;
    historicalFailure(error);
  } finally {
    historicalRequestActive = false;
    updateHistoricalControls();
  }
});
resetHistoricalContracts();

let replaySessionId = null;
let replayLastSequence = 0;
let replayPollTimer = null;
let replayRenderedTrades = [];
const replayTerminalStates = new Set(["completed","stopped","failed"]);
const replayRunningStates = new Set(["validating","running","stopping"]);

function renderReplayTrades(trades) {
  const body = $("replay-trades");
  if(!trades.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No completed replay trades.</td></tr>';
    return;
  }
  body.innerHTML = trades.map(t =>
    `<tr><td>${escapeHtml(t.session_date)}</td><td>${escapeHtml(t.direction)}</td>` +
    `<td>${Number(t.entry_price).toFixed(2)}</td><td>${Number(t.exit_price).toFixed(2)}</td>` +
    `<td>${escapeHtml(t.exit_reason)}</td><td>${Number(t.gross_pnl_points).toFixed(2)}</td></tr>`
  ).join("");
}

function renderReplayEvent(event) {
  const list = $("replay-events");
  if(list.children.length === 1 && list.children[0].classList.contains("muted")) list.innerHTML = "";
  const detail = event.signal || event.trade || {};
  const description = event.type === "signal" ?
    `${detail.direction} signal · ${detail.accepted ? "entry eligible" : detail.reason}` :
    event.type === "entry" ? `${detail.direction} simulated index entry at ${Number(detail.entry_price).toFixed(2)}` :
    event.type === "exit" ? `${detail.direction} simulated exit · ${detail.exit_reason} · ${Number(detail.gross_pnl_points).toFixed(2)} points` :
    event.type;
  const item = document.createElement("li");
  item.textContent = `${kolkataTime(event.timestamp)} · ${description}`;
  list.appendChild(item);
  while(list.children.length > 100) list.removeChild(list.firstElementChild);
  if(event.type === "exit") {
    replayRenderedTrades.push(event.trade);
    renderReplayTrades(replayRenderedTrades);
  }
}

function renderReplayStatus(status) {
  $("replay-status").textContent = status.status;
  $("replay-status").dataset.state = status.status;
  $("replay-progress").value = Number(status.progress_percent || 0);
  $("replay-progress-percent").textContent = `${Number(status.progress_percent || 0).toFixed(1)}%`;
  $("replay-progress-text").textContent = status.total_bars ?
    `${Number(status.processed_bars).toLocaleString("en-IN")} / ${Number(status.total_bars).toLocaleString("en-IN")} validated candles` :
    status.status === "validating" ? "Validating and acquiring historical candles…" : "No candles processed";
  if(status.current_timestamp) {
    $("replay-progress-text").textContent += ` · ${kolkataTime(status.current_timestamp)}`;
  }
  const position = status.open_position;
  $("replay-position").textContent = position ?
    `${position.direction} · ${Number(position.entry_price).toFixed(2)} → ${Number(position.mark_price).toFixed(2)}` :
    "None";
  $("replay-position-pnl").textContent = position ?
    `Unrealized ${Number(position.unrealized_points).toFixed(2)} underlying points · stop ${Number(position.stop_price).toFixed(2)} · target ${Number(position.target_price).toFixed(2)}` :
    "No open simulated index position";
  $("replay-position-pnl").className = position ?
    (Number(position.unrealized_points) >= 0 ? "positive" : "negative") : "muted";
  $("replay-start").disabled = replayRunningStates.has(status.status);
  $("replay-stop").disabled = !["validating","running"].includes(status.status);
  if(status.error) {
    $("replay-message").textContent = status.error;
    $("replay-message").className = "negative";
  } else if(status.status === "stopping") {
    $("replay-message").textContent = "Stop requested; no later candle will be processed.";
    $("replay-message").className = "muted";
  } else if(status.status === "completed") {
    $("replay-message").textContent = "Replay complete. Results are underlying-index points, not options P&L.";
    $("replay-message").className = "positive";
  } else if(status.status === "stopped") {
    $("replay-message").textContent = "Replay stopped. Partial results are not presented as a completed backtest.";
    $("replay-message").className = "muted";
  } else if(status.data_quality && status.data_quality.passed) {
    const quality = status.data_quality;
    const source = quality.retrieval_mode || "validated historical data";
    $("replay-message").textContent =
      `Data quality passed · ${Number(quality.row_count || 0).toLocaleString("en-IN")} candles · ${source}. Yahoo candle timestamp labeling remains an unverified assumption.`;
    $("replay-message").className = "muted";
  }
}

function renderReplayMetrics(metrics) {
  if(!metrics) return;
  $("replay-trade-count").textContent = metrics.completed_trades;
  $("replay-event-count").textContent = metrics.raw_breakout_events;
  $("replay-points").textContent = Number(metrics.gross_underlying_points).toFixed(2);
  $("replay-points").className = Number(metrics.gross_underlying_points) >= 0 ? "positive" : "negative";
  $("replay-session-count").textContent = metrics.eligible_sessions;
}

async function pollReplay(sessionId) {
  if(replaySessionId !== sessionId) return;
  try {
    const status = await api(`/api/replay/${encodeURIComponent(sessionId)}`);
    if(replaySessionId !== sessionId) return;
    renderReplayStatus(status);
    const batch = await api(`/api/replay/${encodeURIComponent(sessionId)}/events?after=${replayLastSequence}`);
    if(replaySessionId !== sessionId) return;
    for(const event of batch.events) {
      replayLastSequence = Math.max(replayLastSequence, Number(event.event_seq));
      renderReplayEvent(event);
    }
    if(status.status === "completed") {
      const results = await api(`/api/replay/${encodeURIComponent(sessionId)}/results`);
      if(replaySessionId !== sessionId) return;
      renderReplayMetrics(results.metrics);
      replayRenderedTrades = results.trades || [];
      renderReplayTrades(replayRenderedTrades);
    }
    if(replayTerminalStates.has(status.status)) {
      clearTimeout(replayPollTimer);
      replayPollTimer = null;
      return;
    }
    replayPollTimer = setTimeout(() => pollReplay(sessionId), 500);
  } catch(err) {
    if(replaySessionId !== sessionId) return;
    $("replay-message").textContent = `Replay status unavailable: ${err.message}`;
    $("replay-message").className = "negative";
    replayPollTimer = setTimeout(() => pollReplay(sessionId), 1500);
  }
}

function attachReplay(sessionId) {
  replaySessionId = sessionId;
  replayLastSequence = 0;
  replayRenderedTrades = [];
  $("replay-events").innerHTML = "";
  renderReplayTrades([]);
  $("replay-trade-count").textContent = "—";
  $("replay-event-count").textContent = "—";
  $("replay-points").textContent = "—";
  $("replay-session-count").textContent = "—";
  clearTimeout(replayPollTimer);
  pollReplay(sessionId);
}

$("replay-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  const ratio = String(values.get("opening_range_atr_max_ratio") || "").trim();
  const payload = {
    start: values.get("start"),
    end: values.get("end"),
    offline: form.elements.offline.checked,
    allow_stale_cache: form.elements.allow_stale_cache.checked,
    ema50_filter: form.elements.ema50_filter.checked,
    opening_range_atr_max_ratio: ratio ? Number(ratio) : null,
  };
  $("replay-start").disabled = true;
  $("replay-message").textContent = "Starting replay; historical data must pass validation first…";
  $("replay-message").className = "muted";
  try {
    const session = await api("/api/replay/start", {method:"POST", body:JSON.stringify(payload)});
    attachReplay(session.session_id);
  } catch(err) {
    $("replay-start").disabled = false;
    $("replay-status").textContent = "Failed to start";
    $("replay-message").textContent = err.message;
    $("replay-message").className = "negative";
  }
});
$("replay-form").elements.offline.addEventListener("change", event => {
  $("replay-form").elements.allow_stale_cache.disabled = !event.currentTarget.checked;
  if(!event.currentTarget.checked) $("replay-form").elements.allow_stale_cache.checked = false;
});
$("replay-stop").addEventListener("click", async () => {
  if(!replaySessionId) return;
  $("replay-stop").disabled = true;
  try {
    const status = await api(`/api/replay/${encodeURIComponent(replaySessionId)}/stop`, {method:"POST"});
    renderReplayStatus(status);
  } catch(err) {
    $("replay-message").textContent = err.message;
    $("replay-message").className = "negative";
  }
});

async function restoreReplaySession() {
  try {
    const sessions = await api("/api/replay");
    const session = [...sessions].reverse().find(item => replayRunningStates.has(item.status)) ||
      [...sessions].reverse().find(item => replayTerminalStates.has(item.status));
    if(session) attachReplay(session.session_id);
  } catch(err) {
    $("replay-message").textContent = `Existing replay status unavailable: ${err.message}`;
    $("replay-message").className = "negative";
  }
}
restoreReplaySession();

/* ---------- Historical options backtest (read-only Groww data) ---------- */
let obRunId = null;
let obTimer = null;
const OB_LABELS = {running:"Running", completed:"Completed", stopped:"Stopped (partial)", failed:"Failed", idle:"No run"};
const OB_ERROR_LABELS = {
  provider_auth:"Authentication failed", provider_denied:"Access denied", provider_not_connected:"Not connected",
  configuration:"Invalid configuration", provider_rate_limited:"Rate limited", provider_response:"Malformed provider response",
  provider_unavailable:"Provider unavailable", execution_error:"Execution error"
};
function obStatus(label, state, message) {
  $("ob-status").textContent = label;
  $("ob-status").dataset.state = state;
  if(message !== undefined) $("ob-message").textContent = message;
}
function obRow(cells) {
  return `<tr>${cells.map(c => `<td>${escapeHtml(c ?? "—")}</td>`).join("")}</tr>`;
}
function obTime(iso) { return iso ? String(iso).replace("T", " ") : "—"; }
function obBody(form) {
  const f = new FormData(form);
  const num = (k) => Number(f.get(k));
  const t = (k) => String(f.get(k)).length === 5 ? `${f.get(k)}:00` : String(f.get(k));
  return {
    start_date: f.get("start_date"), end_date: f.get("end_date"),
    candle_interval: f.get("candle_interval"),
    option_side_mode: f.get("option_side_mode"),
    exit_level_mode: f.get("exit_level_mode"),
    opening_range_minutes: num("opening_range_minutes"),
    entry_cutoff: t("entry_cutoff"), square_off_time: t("square_off_time"),
    strike_offset_steps: num("strike_offset_steps"), min_days_to_expiry: num("min_days_to_expiry"),
    stop_loss_pct: num("stop_loss_pct"), target_pct: num("target_pct"),
    stop_loss_points: num("stop_loss_points"), target_points: num("target_points"),
    max_trades_per_session: num("max_trades_per_session"),
    lots: num("lots"), lot_size: num("lot_size"), initial_capital_inr: num("initial_capital_inr"),
    costs: {
      brokerage_per_order_inr: num("brokerage_per_order_inr"), stt_sell_pct: num("stt_sell_pct"),
      exchange_txn_pct: num("exchange_txn_pct"), stamp_duty_buy_pct: num("stamp_duty_buy_pct"),
      gst_pct: num("gst_pct"), slippage_per_unit_inr: num("slippage_per_unit_inr")
    }
  };
}
function obSyncExitMode() {
  const mode = $("ob-form").elements.exit_level_mode.value;
  document.querySelectorAll("[data-ob-exit-mode]").forEach(label => {
    label.hidden = label.dataset.obExitMode !== mode;
  });
}
function obRenderResult(run) {
  const result = run.result;
  if(!result) return;
  const s = result.summary;
  const kinds = {
    trades: "Trades simulated.",
    zero_trades: "Zero trades: sessions were processed but no entry rule produced a trade. This is not a failure.",
    no_historical_data: "No historical data was returned for any requested session.",
    invalid_or_incomplete_data: "Data was invalid or incomplete for every session; no results can be relied on."
  };
  const cards = [
    ["Sessions processed / requested", `${s.sessions_processed} / ${s.sessions_requested}`],
    ["Excluded sessions", `${s.sessions_excluded} ${Object.keys(s.excluded_reasons).length ? "(" + Object.entries(s.excluded_reasons).map(([k,v]) => `${k}: ${v}`).join(", ") + ")" : ""}`],
    ["Trades", `${s.trade_count} (win rate ${s.win_rate_pct === null ? "n/a" : s.win_rate_pct + "%"})`],
    ["Gross P&L", inr(s.gross_pnl)], ["Total costs", `${inr(s.total_costs)} (charges ${inr(s.total_charges)}, slippage ${inr(s.total_slippage)})`],
    ["Net P&L", inr(s.net_pnl)], ["Max drawdown", `${inr(s.max_drawdown_inr)} (${s.max_drawdown_pct}%)`],
    ["Profit factor", s.profit_factor === null ? "n/a" : String(s.profit_factor)],
    ["Avg net / trade", s.average_trade_pnl === null ? "n/a" : inr(s.average_trade_pnl)],
    ["Simulated equity", `${inr(s.initial_equity)} → ${inr(s.ending_equity)}`]
  ];
  $("ob-summary").hidden = false;
  $("ob-summary").innerHTML = cards.map(([k, v]) => `<div><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("");
  const notes = [kinds[s.result_kind] || "", s.net_pnl_basis, s.profit_factor_note, s.max_drawdown_method];
  if(s.results_may_be_incomplete) notes.push("Some sessions or signals were excluded for data reasons; results may be biased or incomplete.");
  if(!s.costs_configured) notes.push("No costs configured: net P&L is not cost-modelled.");
  $("ob-notes").innerHTML = notes.filter(Boolean).map(n => `<p>${escapeHtml(n)}</p>`).join("");
  $("ob-trades").innerHTML = result.trades.length ? result.trades.map(t => obRow([
    t.session_date, t.contract.groww_symbol, t.contract.option_type, t.contract.strike, t.contract.expiry,
    obTime(t.entry_time), t.entry_premium, obTime(t.exit_time), t.exit_premium,
    `${t.max_premium_reached} @ ${obTime(t.max_premium_time)}`,
    `${t.lots}×${t.lot_size}=${t.quantity}`, inr(t.gross_pnl), inr(t.costs.charges_total), inr(t.costs.slippage), inr(t.net_pnl),
    t.exit_reason, t.warnings.join(" | ")
  ])).join("") : `<tr><td colspan="17" class="muted">No trades</td></tr>`;
  $("ob-sessions").innerHTML = result.sessions.length ? result.sessions.map(x => {
    const skipped = result.skipped_signals.filter(k => k.session_date === x.session_date).map(k => `${k.reason}`);
    return obRow([x.session_date, x.status, [x.reason, ...x.warnings, ...skipped].filter(Boolean).join(" | "), x.signals.length, x.trades.length]);
  }).join("") : `<tr><td colspan="5" class="muted">No sessions</td></tr>`;
  $("ob-rules").innerHTML = [...result.rules, ...result.limitations, result.data_source.timestamp_note]
    .map(r => `<li>${escapeHtml(r)}</li>`).join("");
}
function obApply(run) {
  const running = run.state === "running";
  $("ob-start").disabled = running;
  $("ob-stop").disabled = !running;
  if(running) {
    const p = run.progress;
    obStatus(OB_LABELS.running, "running", `Processed ${p.done} of ${p.total} sessions${p.current_date ? " (now " + p.current_date + ")" : ""}.`);
    return;
  }
  if(run.state === "failed") {
    const e = run.error || {};
    obStatus(OB_ERROR_LABELS[e.category] || "Failed", "failed", `Backtest failed: ${e.message || "unknown error"}. This is not a zero-trade result.`);
    return;
  }
  const kind = run.result && run.result.summary.result_kind;
  const label = run.state === "stopped" ? OB_LABELS.stopped : (kind === "zero_trades" ? "Zero trades" : kind === "no_historical_data" ? "No data" : kind === "invalid_or_incomplete_data" ? "Invalid data" : OB_LABELS.completed);
  obStatus(label, run.state === "stopped" ? "incomplete" : (kind === "trades" ? "ok" : "incomplete"), run.state === "stopped" ? "Stopped; results cover only the sessions processed so far." : "Backtest finished.");
  obRenderResult(run);
}
async function obPoll() {
  if(!obRunId) return;
  try {
    const run = await api(`/api/options-backtest/runs/${encodeURIComponent(obRunId)}`);
    obApply(run);
    if(run.state === "running") obTimer = setTimeout(obPoll, 2000);
  } catch(err) {
    $("ob-start").disabled = false; $("ob-stop").disabled = true;
    obStatus("Failed", "failed", `Could not read run status: ${err.message}`);
  }
}
$("ob-form").elements.exit_level_mode.addEventListener("change", obSyncExitMode);
obSyncExitMode();
$("ob-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearTimeout(obTimer);
  $("ob-summary").hidden = true; $("ob-notes").textContent = "";
  $("ob-trades").innerHTML = `<tr><td colspan="17" class="muted">No trades</td></tr>`;
  $("ob-sessions").innerHTML = `<tr><td colspan="5" class="muted">No sessions</td></tr>`;
  $("ob-rules").innerHTML = "";
  $("ob-start").disabled = true;
  obStatus("Starting", "running", "Starting backtest…");
  try {
    const run = await api("/api/options-backtest/runs", {method:"POST", body: JSON.stringify(obBody(event.target))});
    obRunId = run.run_id;
    obApply(run);
    obTimer = setTimeout(obPoll, 1000);
  } catch(err) {
    $("ob-start").disabled = false;
    const label = err.status === 409 ? "Not connected" : err.status === 422 ? "Invalid configuration" : "Failed";
    obStatus(label, "failed", err.message);
  }
});
$("ob-stop").addEventListener("click", async () => {
  if(!obRunId) return;
  $("ob-stop").disabled = true;
  try { await api(`/api/options-backtest/runs/${encodeURIComponent(obRunId)}/stop`, {method:"POST"}); }
  catch(err) { obStatus("Failed", "failed", err.message); }
});

// Groww historical response log (diagnostic).
async function rlRefresh() {
  const box = $("rl-entries");
  try {
    const data = await api("/api/provider/response-log");
    $("rl-status").textContent = `${data.entries.length} response(s)`;
    if(!data.entries.length) { box.textContent = "No Groww historical responses recorded yet."; return; }
    box.innerHTML = data.entries.map(e => {
      const query = Object.entries(e.query).map(([k, v]) => `${k}=${v}`).join("&");
      const head = `#${e.id} ? ${e.received_at} ? HTTP ${e.http_status} ? ${e.path}`;
      return `<details><summary>${escapeHtml(head)}</summary>`
        + `<p class="muted">${escapeHtml(query)}</p>`
        + `<p class="muted">Structure: ${escapeHtml(JSON.stringify(e.structure))}</p>`
        + `<pre>${escapeHtml(JSON.stringify(e.body, null, 2))}</pre></details>`;
    }).join("");
  } catch(err) {
    $("rl-status").textContent = "Failed";
    box.textContent = err.message;
  }
}
$("rl-refresh").addEventListener("click", rlRefresh);
$("rl-clear").addEventListener("click", async () => {
  await api("/api/provider/response-log/clear", {method: "POST"}).catch(() => null);
  rlRefresh();
});
document.querySelectorAll('.app-tab[data-tab="log"]').forEach(b => b.addEventListener("click", rlRefresh));
