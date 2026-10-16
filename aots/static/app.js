const $ = (id) => document.getElementById(id);
const inr = (n) => new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2}).format(Number(n||0));
async function api(path, options={}) {
  const res = await fetch(path,{headers:{"Content-Type":"application/json"},...options});
  const body = await res.json().catch(()=>({detail:"Unexpected response"}));
  if(!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}
async function refresh() {
  try {
    const [status,risk,trades] = await Promise.all([api("/api/status"),api("/api/risk"),api("/api/trades")]);
    $("health-label").textContent = "System healthy · paper only";
    $("capital").textContent = inr(status.capital_inr);
    $("pnl").textContent = inr(risk.daily_pnl);
    $("pnl").className = risk.daily_pnl >= 0 ? "positive" : "negative";
    $("positions-count").textContent = risk.open_positions;
    $("loss-limit").textContent = inr(risk.max_daily_loss_inr);
    $("trade-risk").textContent = `${(risk.max_trade_risk_pct*100).toFixed(2)}% · ${inr(risk.max_trade_risk_inr)}`;
    $("loss-streak").textContent = `${risk.consecutive_losses} / ${risk.max_consecutive_losses}`;
    $("daily-pnl").textContent = inr(risk.daily_pnl);
    $("kill-status").textContent = risk.kill_switch ? "ON" : "OFF";
    $("risk-badge").textContent = risk.kill_switch ? "Entries blocked" : risk.paused_for_day ? "Paused" : "Active";
    $("risk-badge").style.background = risk.kill_switch || risk.paused_for_day ? "#49242c" : "#183b35";
    $("risk-badge").style.color = risk.kill_switch || risk.paused_for_day ? "#ffb7bd" : "#81e2c0";
    $("kill-toggle").textContent = risk.kill_switch ? "Disable kill switch" : "Enable kill switch";
    $("kill-toggle").className = risk.kill_switch ? "" : "danger";
    renderTrades(trades);
  } catch(e) { $("health-label").textContent = `API unavailable: ${e.message}`; }
}
function renderTrades(trades) {
  const body = $("trades-body");
  if(!trades.length){body.innerHTML='<tr><td colspan="7" class="muted">No paper trades yet. Create a simulated trade above to test the workflow.</td></tr>';return;}
  body.innerHTML = trades.map(t => {
    const pnl = t.status==="OPEN" ? Number(t.unrealised_pnl) : Number(t.realised_pnl);
    const pnlClass = pnl >= 0 ? "positive" : "negative";
    const actions = t.status==="OPEN" ?
      `<button data-action="mark" data-id="${t.trade_id}">Mark</button><button data-action="close" data-id="${t.trade_id}">Close</button>` : "—";
    return `<tr><td>${escapeHtml(t.symbol)}</td><td>${t.status}</td><td>${t.quantity} (${t.lots} lot)</td><td>${inr(t.entry_price)}</td><td>${inr(t.mark_price)}</td><td class="${pnlClass}">${inr(pnl)}</td><td>${actions}</td></tr>`;
  }).join("");
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
$("refresh").addEventListener("click",refresh);
$("kill-toggle").addEventListener("click",async()=>{
  try { const r=await api("/api/risk"); await api("/api/risk/kill-switch",{method:"POST",body:JSON.stringify({enabled:!r.kill_switch})}); await refresh(); }
  catch(e){alert(e.message);}
});
$("trade-form").addEventListener("submit",async(e)=>{
  e.preventDefault(); const f=new FormData(e.currentTarget); const data=Object.fromEntries(f.entries());
  ["entry_price","stop_price","quantity","lots","market_data_age_seconds"].forEach(k=>data[k]=Number(data[k]));
  try { await api("/api/paper/trade",{method:"POST",body:JSON.stringify(data)}); $("form-message").textContent="Simulated trade created."; $("form-message").className="positive"; await refresh(); }
  catch(err){$("form-message").textContent=err.message;$("form-message").className="negative";}
});
$("trades-body").addEventListener("click",async(e)=>{
  const b=e.target.closest("button"); if(!b)return;
  try{
    if(b.dataset.action==="mark"){
      const raw=prompt("Enter simulated option mark price (₹):","98"); if(raw===null)return;
      await api("/api/paper/mark",{method:"POST",body:JSON.stringify({trade_id:b.dataset.id,mark_price:Number(raw)})});
    } else {
      const raw=prompt("Enter simulated exit price (₹):","98"); if(raw===null)return;
      await api(`/api/paper/close/${b.dataset.id}?exit_price=${encodeURIComponent(Number(raw))}`,{method:"POST"});
    }
    await refresh();
  }catch(err){alert(err.message);}
});
refresh();
