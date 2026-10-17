const $ = (id) => document.getElementById(id);
const inr = (n) => new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2}).format(Number(n||0));
let pendingEntryKey = null;
let entryRequestActive = false;
async function api(path, options={}) {
  const res = await fetch(path,{headers:{"Content-Type":"application/json"},...options});
  const body = await res.json().catch(()=>({detail:"Unexpected response"}));
  if(!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}
async function refresh() {
  try {
    const [status,risk,trades,decisions] = await Promise.all([api("/api/status"),api("/api/risk"),api("/api/trades"),api("/api/strategy/decisions")]);
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
    renderStrategyDecisions(decisions);
  } catch(e) { $("health-label").textContent = `API unavailable: ${e.message}`; }
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
