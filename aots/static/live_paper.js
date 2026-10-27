(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("live-paper-form");
  const tab = document.querySelector('.app-tab[data-tab="live-paper"]');
  let polling = false;
  let refreshing = false;

  function showMessage(message, isError = false) {
    const element = $("live-paper-message");
    element.textContent = message;
    element.classList.toggle("negative", isError);
    element.classList.toggle("positive", !isError);
  }

  function localDate() {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function renderEvents(events) {
    const list = $("live-paper-events");
    list.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "No live-paper events.";
      list.append(empty);
      return;
    }
    for (const event of events) {
      const item = document.createElement("li");
      const detail = document.createElement("span");
      detail.className = "muted";
      let detailText = "";
      try {
        const parsed = JSON.parse(event.detail_json || "{}");
        detailText = JSON.stringify(parsed);
      } catch {
        detailText = "Event details unavailable";
      }
      detail.textContent = ` · ${event.timestamp || ""} · ${detailText}`;
      item.textContent = event.event_type;
      item.append(detail);
      list.append(item);
    }
  }

  async function refreshLivePaper() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [status, events] = await Promise.all([
        api("/api/live-paper/status"),
        api("/api/live-paper/events"),
      ]);
      $("live-paper-status").textContent = status.status;
      $("live-paper-date").textContent = status.session_date || "—";
      $("live-paper-index").textContent = status.last_index_price == null
        ? "—"
        : Number(status.last_index_price).toLocaleString("en-IN");
      $("live-paper-index-time").textContent = status.last_index_time || "No feed observation yet";
      $("live-paper-candles").textContent = String(status.completed_index_candles || 0);
      $("live-paper-range").textContent = status.opening_range
        ? `${status.opening_range.high} / ${status.opening_range.low}`
        : "—";
      $("live-paper-range-error").textContent = status.opening_range_error || "";
      $("live-paper-count").textContent = String(status.trade_count || 0);
      const position = status.active_trade;
      $("live-paper-position").textContent = position
        ? `${position.symbol} · ${position.quantity} units`
        : "None";
      $("live-paper-position-pnl").textContent = position
        ? `Unrealised ${Number(position.unrealised_pnl || 0).toFixed(2)} · ${position.status}`
        : "";
      $("live-paper-start").disabled = !status.can_start;
      $("live-paper-resume").disabled = !status.can_resume;
      $("live-paper-stop").disabled = ![
        "RUNNING", "STARTING", "RUNNING_POSITION_RECOVERY",
        "STOPPING_POSITION_OPEN", "SESSION_END_EXIT_PENDING", "FEED_ERROR",
      ].includes(status.status);
      if (status.failure_reason) showMessage(status.failure_reason, true);
      else if (status.status === "RUNNING") showMessage("Live market data connected; simulated trading is active.");
      else if (status.status === "RECOVERY_REQUIRED") showMessage("An open paper position needs explicit recovery after reconnect.", true);
      renderEvents(events);
    } catch (error) {
      showMessage(error.message || "Unable to load live-paper status.", true);
    } finally {
      refreshing = false;
    }
  }

  function numberValue(data, name) {
    return Number(data.get(name));
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(form);
    const request = {
      expiry_date: data.get("expiry_date"),
      opening_range_minutes: numberValue(data, "opening_range_minutes"),
      candle_interval_minutes: numberValue(data, "candle_interval_minutes"),
      option_side_mode: data.get("option_side_mode"),
      entry_cutoff: data.get("entry_cutoff"),
      square_off_time: data.get("square_off_time"),
      strike_step: numberValue(data, "strike_step"),
      strike_offset_steps: numberValue(data, "strike_offset_steps"),
      lots: numberValue(data, "lots"),
      lot_size: numberValue(data, "lot_size"),
      stop_loss_points: numberValue(data, "stop_loss_points"),
      take_profit_points: data.get("take_profit_points")
        ? numberValue(data, "take_profit_points")
        : null,
      max_trades_per_session: numberValue(data, "max_trades_per_session"),
    };
    $("live-paper-start").disabled = true;
    showMessage("Starting the read-only Groww feed…");
    try {
      await api("/api/live-paper/start", {
        method: "POST",
        body: JSON.stringify(request),
      });
      showMessage("Live paper session started.");
      await refreshLivePaper();
    } catch (error) {
      showMessage(error.message || "Live paper could not be started.", true);
      $("live-paper-start").disabled = false;
    }
  });

  $("live-paper-stop").addEventListener("click", async () => {
    try {
      const result = await api("/api/live-paper/stop", {method: "POST"});
      showMessage(result.failure_reason || `Session status: ${result.status}`);
      await refreshLivePaper();
    } catch (error) {
      showMessage(error.message || "Live paper could not be stopped.", true);
    }
  });

  $("live-paper-resume").addEventListener("click", async () => {
    try {
      await api("/api/live-paper/resume", {method: "POST"});
      showMessage("Open paper position is being recovered.");
      await refreshLivePaper();
    } catch (error) {
      showMessage(error.message || "Open paper position could not be recovered.", true);
    }
  });

  tab.addEventListener("click", () => {
    void refreshLivePaper();
    if (!polling) {
      polling = true;
      window.setInterval(() => {
        if (tab.getAttribute("aria-pressed") === "true") void refreshLivePaper();
      }, 2000);
    }
  });

  form.elements.expiry_date.value = localDate();
  void refreshLivePaper();
})();
