(() => {
  "use strict";

  const REFRESH_MS = 30_000;
  const root = document.getElementById("dashboard");
  const configuredTimeZone = root.dataset.timeZone || "UTC";
  let snapshot = {};
  let refreshTimer = null;
  let nextRefreshAt = 0;
  let requestInFlight = false;
  let updateError = "";
  let tooltipSequence = 0;
  let activeTooltipTrigger = null;
  let pinnedTooltipTrigger = null;

  const escapeHtml = (value) => String(value ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const number = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const formatDuration = (value) => {
    if (value === null || value === undefined || value === "") return "—";
    let seconds = Math.max(0, Math.round(number(value)));
    const hours = Math.floor(seconds / 3600);
    seconds -= hours * 3600;
    const minutes = Math.floor(seconds / 60);
    seconds -= minutes * 60;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes || hours) parts.push(`${minutes}m`);
    parts.push(`${seconds}s`);
    return parts.join(" ");
  };

  const parseTime = (value) => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };

  const relativeTime = (value) => {
    const date = parseTime(value);
    if (!date) return "—";
    const deltaSeconds = Math.round((date.getTime() - Date.now()) / 1000);
    const future = deltaSeconds > 0;
    const absolute = Math.abs(deltaSeconds);
    if (absolute < 10) return future ? "in a few seconds" : "just now";
    if (absolute < 60) return future ? `in ${absolute}s` : `${absolute}s ago`;
    const minutes = Math.round(absolute / 60);
    if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`;
    const hours = Math.round(absolute / 3600);
    if (hours < 24) return future ? `in ${hours}h` : `${hours}h ago`;
    const days = Math.round(absolute / 86400);
    return future ? `in ${days}d` : `${days}d ago`;
  };

  const exactTime = (value) => {
    const date = parseTime(value);
    if (!date) return "—";
    const options = {
      timeZone: configuredTimeZone,
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    };
    try {
      return new Intl.DateTimeFormat("en-GB", options).format(date);
    } catch (_error) {
      return new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(date);
    }
  };

  const timeMarkup = (value, extraClass = "") => {
    if (!parseTime(value)) return '<span class="duration">—</span>';
    return `<time class="relative-time ${escapeHtml(extraClass)}" datetime="${escapeHtml(value)}" title="${escapeHtml(exactTime(value))}">${escapeHtml(relativeTime(value))}</time>`
      + `<span class="time-exact">${escapeHtml(exactTime(value))}</span>`;
  };

  const labelForState = (state) => String(state || "unknown").replaceAll("_", " ");

  const mediaDetail = (row) => {
    const parts = [row.episodeCode, row.episodeTitle].filter(Boolean);
    return parts.join(" · ");
  };

  const mediaMarkup = (row) => {
    const detail = mediaDetail(row);
    return `<span class="media-title">${escapeHtml(row.title || "Unknown")}</span>`
      + (detail ? `<span class="media-detail">${escapeHtml(detail)}</span>` : "");
  };

  const statusMarkup = (state, reason = "") => {
    const clean = String(state || "unknown");
    const label = labelForState(clean);
    if (!reason) {
      return `<span class="badge ${escapeHtml(clean)}">${escapeHtml(label)}</span>`;
    }
    tooltipSequence += 1;
    const tooltipId = `status-reason-${tooltipSequence}`;
    return `<span class="status-with-tooltip">`
      + `<button class="badge badge-tooltip-trigger ${escapeHtml(clean)}" type="button" `
      + `data-tooltip-trigger aria-describedby="${tooltipId}" aria-expanded="false">${escapeHtml(label)}</button>`
      + `<span class="status-tooltip" id="${tooltipId}" role="tooltip" hidden>`
      + `<strong>Reason</strong><span>${escapeHtml(reason)}</span></span></span>`;
  };

  const exactTimeMarkup = (value) => {
    if (!parseTime(value)) return '<span class="duration">—</span>';
    return `<time class="time-exact-only" datetime="${escapeHtml(value)}">${escapeHtml(exactTime(value))}</time>`;
  };

  const formatRemaining = (seconds) => {
    const value = Math.round(number(seconds));
    return value >= 0 ? formatDuration(value) : `Over by ${formatDuration(Math.abs(value))}`;
  };

  const detailedReason = (row) => {
    const detail = row?.failureDetails || {};
    const parts = [
      row?.reason,
      detail.errorMessage,
      ...(Array.isArray(detail.events) ? detail.events.slice(-2) : []),
    ].filter(Boolean);
    return parts.join(" | ");
  };

  const tooltipFor = (trigger) => {
    const id = trigger?.getAttribute("aria-describedby");
    return id ? document.getElementById(id) : null;
  };

  const positionTooltip = (trigger) => {
    const tooltip = tooltipFor(trigger);
    if (!tooltip || tooltip.hidden) return;
    const margin = 8;
    const viewportPadding = 12;
    const triggerRect = trigger.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const roomAbove = triggerRect.top - margin - tooltipRect.height >= viewportPadding;
    const roomBelow = triggerRect.bottom + margin + tooltipRect.height <= window.innerHeight - viewportPadding;
    const placeAbove = roomAbove && !roomBelow;
    const top = placeAbove
      ? triggerRect.top - tooltipRect.height - margin
      : Math.min(triggerRect.bottom + margin, window.innerHeight - tooltipRect.height - viewportPadding);
    const idealLeft = triggerRect.left + (triggerRect.width / 2) - (tooltipRect.width / 2);
    const left = Math.min(
      Math.max(idealLeft, viewportPadding),
      window.innerWidth - tooltipRect.width - viewportPadding,
    );
    const arrowLeft = Math.min(
      Math.max(triggerRect.left + (triggerRect.width / 2) - left, 12),
      tooltipRect.width - 12,
    );
    tooltip.classList.toggle("is-above", placeAbove);
    tooltip.style.top = `${Math.max(viewportPadding, top)}px`;
    tooltip.style.left = `${Math.max(viewportPadding, left)}px`;
    tooltip.style.setProperty("--tooltip-arrow-left", `${arrowLeft}px`);
  };

  const closeTooltip = (trigger = activeTooltipTrigger) => {
    const tooltip = tooltipFor(trigger);
    if (tooltip) tooltip.hidden = true;
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (activeTooltipTrigger === trigger) activeTooltipTrigger = null;
    if (pinnedTooltipTrigger === trigger) pinnedTooltipTrigger = null;
  };

  const openTooltip = (trigger, pinned = false) => {
    if (!trigger) return;
    if (activeTooltipTrigger && activeTooltipTrigger !== trigger) {
      closeTooltip(activeTooltipTrigger);
    }
    const tooltip = tooltipFor(trigger);
    if (!tooltip) return;
    tooltip.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    activeTooltipTrigger = trigger;
    pinnedTooltipTrigger = pinned ? trigger : null;
    positionTooltip(trigger);
  };

  const bindTooltipEvents = () => {
    document.addEventListener("mouseover", (event) => {
      const trigger = event.target.closest?.("[data-tooltip-trigger]");
      if (trigger && !trigger.contains(event.relatedTarget)) openTooltip(trigger);
    });
    document.addEventListener("mouseout", (event) => {
      const trigger = event.target.closest?.("[data-tooltip-trigger]");
      if (
        trigger
        && !trigger.contains(event.relatedTarget)
        && pinnedTooltipTrigger !== trigger
        && document.activeElement !== trigger
      ) {
        closeTooltip(trigger);
      }
    });
    document.addEventListener("focusin", (event) => {
      const trigger = event.target.closest?.("[data-tooltip-trigger]");
      if (trigger) openTooltip(trigger, pinnedTooltipTrigger === trigger);
    });
    document.addEventListener("focusout", (event) => {
      const trigger = event.target.closest?.("[data-tooltip-trigger]");
      if (
        trigger
        && pinnedTooltipTrigger !== trigger
        && !trigger.matches(":hover")
      ) {
        closeTooltip(trigger);
      }
    });
    document.addEventListener("click", (event) => {
      const trigger = event.target.closest?.("[data-tooltip-trigger]");
      if (!trigger) {
        closeTooltip();
        return;
      }
      if (pinnedTooltipTrigger === trigger) {
        closeTooltip(trigger);
      } else {
        openTooltip(trigger, true);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !activeTooltipTrigger) return;
      const trigger = activeTooltipTrigger;
      closeTooltip(trigger);
      trigger.focus();
    });
    window.addEventListener("resize", () => positionTooltip(activeTooltipTrigger));
    document.addEventListener("scroll", () => positionTooltip(activeTooltipTrigger), true);
  };

  const table = (rows, kind, emptyMessage) => {
    if (!rows.length) return `<p class="empty-state">${escapeHtml(emptyMessage)}</p>`;
    const types = new Set(rows.map((row) => row.itemType).filter(Boolean));
    const showType = types.size > 1;
    let columns;
    if (kind === "upcoming") {
      columns = [
        ["Position", "position"],
        ["Media", "media"],
        ...(showType ? [["Type", "type"]] : []),
        ["Language", "language"],
        ["Queued", "queued"],
      ];
    } else if (kind === "active") {
      columns = [
        ["Media", "media"],
        ...(showType ? [["Type", "type"]] : []),
        ["Language", "language"],
        ["Status", "status"],
        ["Lane", "lane"],
        ["Elapsed", "elapsed"],
        ["Est. total", "estimate"],
        ["Remaining", "eta"],
        ["Started", "started"],
      ];
    } else {
      columns = [
        ["Media", "media"],
        ...(showType ? [["Type", "type"]] : []),
        ["Language", "language"],
        ["Outcome", "outcome"],
        ["Duration", "duration"],
        ["Estimate", "estimate"],
        ["Lane", "lane"],
        ["Attempts", "attempts"],
        ["Finished", "finished"],
      ];
    }

    const cell = (row, key, index) => {
      if (key === "position") return `<span class="queue-position">#${index + 1}</span>`;
      if (key === "media") return mediaMarkup(row);
      if (key === "type") return escapeHtml(row.itemType === "movies" ? "Movie" : "Episode");
      if (key === "language") return escapeHtml(row.targetLanguage || "—");
      if (key === "status") return statusMarkup(row.state, detailedReason(row));
      if (key === "outcome") {
        const state = row.repaired && row.outcome === "accepted" ? "repaired" : row.outcome;
        return statusMarkup(state, detailedReason(row));
      }
      if (key === "elapsed") {
        const started = row.startedAt || "";
        return `<span class="duration live-duration" data-started-at="${escapeHtml(started)}">${escapeHtml(formatDuration(row.durationSeconds))}</span>`;
      }
      if (key === "duration") return `<span class="duration">${escapeHtml(formatDuration(row.durationSeconds))}</span>`;
      if (key === "estimate") return `<span class="duration">${escapeHtml(formatDuration(row.estimatedSeconds))}</span>`;
      if (key === "eta") {
        if (row.estimatedSeconds === null || row.estimatedSeconds === undefined) {
          return '<span class="duration">—</span>';
        }
        const started = parseTime(row.startedAt);
        const elapsed = started
          ? Math.max(0, (Date.now() - started.getTime()) / 1000)
          : number(row.durationSeconds);
        const hasProgressEstimate = number(row.progress) > 0
          && row.etaSeconds !== null
          && row.etaSeconds !== undefined;
        const remaining = hasProgressEstimate
          ? number(row.etaSeconds)
          : number(row.estimatedSeconds) - elapsed;
        const deadlineAt = Date.now() + remaining * 1000;
        return `<span class="duration live-remaining" data-deadline-at="${deadlineAt}">${escapeHtml(formatRemaining(remaining))}</span>`;
      }
      if (key === "lane") return escapeHtml(row.lane || "—");
      if (key === "attempts") return escapeHtml(row.attempts ?? "—");
      if (key === "queued") return timeMarkup(row.queuedAt);
      if (key === "started") return exactTimeMarkup(row.startedAt);
      if (key === "finished") return timeMarkup(row.timestamp || row.finishedAt);
      return "—";
    };

    const header = columns.map(([label]) => `<th scope="col">${escapeHtml(label)}</th>`).join("");
    const body = rows.map((row, index) => `<tr>${
      columns.map(([label, key]) => (
        `<td class="cell-${escapeHtml(key)}" data-label="${escapeHtml(label)}">${cell(row, key, index)}</td>`
      )).join("")
    }</tr>`).join("");
    return `<div class="table-wrap"><table class="data-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
  };

  const metric = (label, value, tone = "") => (
    `<div class="metric ${escapeHtml(tone)}"><span class="metric-label">${escapeHtml(label)}</span>`
    + `<strong class="metric-value">${escapeHtml(value)}</strong></div>`
  );

  const panelHeader = (title, note = "") => (
    `<div class="panel-header"><div><h2>${escapeHtml(title)}</h2>`
    + (note ? `<p class="section-note">${note}</p>` : "")
    + "</div></div>"
  );

  const renderHeader = (service, cycle) => {
    const phase = labelForState(service.phase || "startup");
    const error = updateError
      ? `<span class="status-warning" role="status">Update delayed</span>`
      : '<span id="refresh-countdown">Refresh in 30s</span>';
    return `<header class="topbar">
      <div>
        <div class="eyebrow"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(phase)}</div>
        <h1>Translation status</h1>
        <p class="header-meta">
          <span>Cycle #${escapeHtml(cycle.number ?? "—")}</span>
          <span id="freshness">${timeMarkup(snapshot.generatedAt)}</span>
          <span>${error}</span>
        </p>
      </div>
      <div class="header-actions">
        <a class="btn btn-secondary" href="/logs">Logs</a>
        <button class="btn btn-secondary" id="theme-toggle" type="button" aria-label="Switch color theme">Theme</button>
        <button class="btn btn-primary" id="refresh-button" type="button">Refresh now</button>
      </div>
    </header>`;
  };

  const renderOverview = (cycle, service) => {
    const initial = number(cycle.initial);
    const done = number(cycle.done);
    const percent = initial ? Math.round((done / initial) * 100) : 0;
    return `<section class="panel overview" aria-labelledby="cycle-overview-title">
      <div class="overview-grid">
        <div>
          <div class="progress-kicker" id="cycle-overview-title">Current cycle</div>
          <div class="progress-copy">${done.toLocaleString()} of ${initial.toLocaleString()} complete <span>· ${percent}%</span></div>
          <progress max="${Math.max(initial, 1)}" value="${Math.min(done, Math.max(initial, 1))}" aria-label="Cycle completion">${percent}%</progress>
          <div class="overview-facts">
            <div class="fact"><span class="fact-label">Remaining</span><strong class="fact-value">${number(cycle.remaining).toLocaleString()}</strong></div>
            <div class="fact"><span class="fact-label">Elapsed</span><strong class="fact-value">${escapeHtml(formatDuration(cycle.elapsedSeconds))}</strong></div>
            <div class="fact"><span class="fact-label">Approx. ETA</span><strong class="fact-value">${escapeHtml(formatDuration(cycle.etaSeconds))}</strong></div>
            <div class="fact"><span class="fact-label">Next cycle</span><strong class="fact-value">${service.nextCycleAt ? escapeHtml(relativeTime(service.nextCycleAt)) : "—"}</strong></div>
          </div>
        </div>
        <div>
          <div class="metric-group">
            <h3>Pipeline</h3>
            <div class="metric-grid">
              ${metric("Queued", number(cycle.queued).toLocaleString())}
              ${metric("Translating", number(cycle.translating).toLocaleString(), "tone-accent")}
              ${metric("Validating", number(cycle.validating).toLocaleString(), "tone-accent")}
              ${metric("Repairing", number(cycle.repairing).toLocaleString(), "tone-warning")}
            </div>
          </div>
          <div class="metric-group">
            <h3>Outcomes</h3>
            <div class="metric-grid outcomes">
              ${metric("Accepted", number(cycle.accepted).toLocaleString(), "tone-success")}
              ${metric("Failed", number(cycle.failed).toLocaleString(), "tone-danger")}
              ${metric("Timed out", number(cycle.timedOut).toLocaleString(), "tone-danger")}
              ${metric("Deferred", number(cycle.deferred).toLocaleString(), "tone-warning")}
              ${metric("Quarantined", number(cycle.quarantined).toLocaleString(), "tone-danger")}
            </div>
          </div>
        </div>
      </div>
    </section>`;
  };

  const renderDiagnostics = (timing, circuits) => {
    const file = timing?.file || {};
    const repair = timing?.repair || {};
    const rate = (entry) => Number.isFinite(Number(entry.secondsPerCue))
      ? `~${Number(entry.secondsPerCue).toFixed(1)} sec/cue`
      : "—";
    const timingBlock = (title, entry) => {
      const samples = number(entry.sampleCount);
      const basis = samples > 0 ? "Learned average" : "Cold-start estimate";
      return `<article class="timing-block">
        <div class="timing-block-copy">
          <span class="timing-kind">${escapeHtml(title)}</span>
          <span class="timing-basis">${basis}</span>
        </div>
        <div class="timing-reading">
          <strong>${escapeHtml(rate(entry))}</strong>
          <span>${samples.toLocaleString()} ${samples === 1 ? "sample" : "samples"}</span>
        </div>
      </article>`;
    };
    const activeCircuits = (circuits || []).filter(
      (entry) => entry.state === "open" || entry.state === "half_open",
    );
    const breakerRows = activeCircuits.map((entry) => {
      const failures = number(entry.failures);
      const retryAt = entry.retryAt && Number.isFinite(Number(entry.retryAt))
        ? new Date(Number(entry.retryAt) * 1000).toISOString()
        : entry.retryAt;
      const trial = retryAt ? ` · Next trial ${timeMarkup(retryAt)}` : "";
      return `<div class="protection-series">
        <strong>${escapeHtml(entry.seriesTitle || entry.seriesKey || "Unknown series")}</strong>
        <span>${failures.toLocaleString()} consecutive ${failures === 1 ? "failure" : "failures"}${trial}</span>
      </div>`;
    }).join("");
    const protection = breakerRows
      ? `<div class="protection-row is-warning" role="status">
          <span class="protection-badge">Protection active</span>
          <div class="protection-copy">
            <strong>Some series are temporarily paused</strong>
            <span>Other translations and cue repairs continue normally.</span>
          </div>
          <div class="protection-series-list">${breakerRows}</div>
        </div>`
      : `<div class="protection-row is-healthy" role="status">
          <span class="protection-badge">Healthy</span>
          <div class="protection-copy">
            <strong>All series available</strong>
            <span>No circuit breakers are limiting translation.</span>
          </div>
        </div>`;
    return `<section class="panel">${panelHeader("Timing & protection", "Adaptive estimates and series circuit-breaker status.")}
      <div class="diagnostics-content">
        <div class="timing-grid">
          ${timingBlock("File translation", file)}
          ${timingBlock("Cue repair", repair)}
        </div>
        ${protection}
      </div>
    </section>`;
  };

  const renderRolling = (history) => {
    const cards = Object.entries(history || {}).map(([window, values]) => {
      const accepted = number(values.accepted);
      const repaired = number(values.repaired);
      const line = (label, key, tone) => {
        const count = number(values[key]);
        return `<div class="outcome-line ${tone} ${count ? "" : "is-zero"}"><span>${escapeHtml(label)}</span><strong>${count.toLocaleString()}</strong></div>`;
      };
      return `<article class="window-card">
        <h3 class="window-title">${escapeHtml(window)}</h3>
        <div class="accepted-summary">${accepted.toLocaleString()} accepted<small>(${repaired.toLocaleString()} repaired)</small></div>
        ${line("Failed", "failed", "tone-danger")}
        ${line("Timed out", "timed_out", "tone-danger")}
        ${line("Deferred", "deferred", "tone-warning")}
        ${line("Quarantined", "quarantined", "tone-danger")}
      </article>`;
    }).join("");
    return `<section class="panel">${panelHeader("Rolling outcomes", "Repaired is included within accepted.")}
      <div class="rolling-grid">${cards}</div>
    </section>`;
  };

  const maintenanceLabels = {
    formatted: "Formatted",
    repaired: "Repaired",
    quarantined: "Quarantined",
    deleted: "Deleted",
    undersized: "Undersized",
    pruned: "Pruned",
    source_less_warnings: "Source-less warnings",
    repeat_quarantines: "Repeat quarantines",
    quarantine_holds: "Quarantine holds",
    variant_outputs: "Variant outputs",
    failures: "Failures",
  };

  const renderMaintenance = (maintenance) => {
    const lastScan = maintenance?.lastScan || null;
    const metrics = lastScan?.metrics || {};
    const nonZero = Object.entries(metrics).filter(([, value]) => number(value) > 0);
    const note = lastScan?.timestamp ? `Scanned ${escapeHtml(relativeTime(lastScan.timestamp))}` : "No scan recorded";
    const content = nonZero.length
      ? `<div class="maintenance-grid">${nonZero.map(([key, value]) => (
        `<div class="maintenance-item"><span class="maintenance-label">${escapeHtml(maintenanceLabels[key] || key.replaceAll("_", " "))}</span>`
        + `<strong class="maintenance-value">${number(value).toLocaleString()}</strong></div>`
      )).join("")}</div>`
      : '<p class="empty-state">No maintenance actions in the latest scan.</p>';
    return `<section class="panel">${panelHeader("Latest maintenance scan", note)}${content}</section>`;
  };

  const render = () => {
    const service = snapshot.service || {};
    const cycle = snapshot.currentCycle || {};
    const active = snapshot.activeJobs || [];
    const upcoming = snapshot.upNext || [];
    const recent = snapshot.recentOutcomes || [];
    activeTooltipTrigger = null;
    pinnedTooltipTrigger = null;
    tooltipSequence = 0;
    root.innerHTML = `<div class="dashboard-shell">
      ${renderHeader(service, cycle)}
      ${renderOverview(cycle, service)}
      ${renderDiagnostics(snapshot.timing || {}, snapshot.circuits || [])}
      <section class="panel">${panelHeader("Active now", `${active.length.toLocaleString()} in progress`)}
        ${table(active, "active", "No active translations or repairs.")}
      </section>
      <section class="panel">${panelHeader("Up next", "Next 10 queued jobs")}
        ${table(upcoming, "upcoming", "No queued jobs.")}
      </section>
      <section class="panel">${panelHeader("Recent outcomes", "Latest completed work")}
        ${table(recent, "recent", "No completed jobs recorded yet.")}
      </section>
      ${renderRolling(snapshot.history || {})}
      ${renderMaintenance(snapshot.maintenance || {})}
      <p class="footer-note">Auto-refreshes every 30 seconds · trusted LAN endpoint · no subtitle text or filesystem paths exposed</p>
    </div>`;
    root.setAttribute("aria-busy", "false");
    bindControls();
    tick();
  };

  const currentTheme = () => document.documentElement.dataset.theme || "dark";

  const updateThemeButton = () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const next = currentTheme() === "dark" ? "light" : "dark";
    button.textContent = `${next === "light" ? "Light" : "Dark"} theme`;
    button.setAttribute("aria-label", `Switch to ${next} theme`);
  };

  const applyInitialTheme = () => {
    let saved = null;
    try {
      saved = localStorage.getItem("dashboard-theme");
    } catch (_error) {
      saved = null;
    }
    const system = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = saved === "light" || saved === "dark" ? saved : system;
  };

  const toggleTheme = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("dashboard-theme", next);
    } catch (_error) {
      // The selected theme still applies for this page load.
    }
    updateThemeButton();
  };

  const bindControls = () => {
    const theme = document.getElementById("theme-toggle");
    const refresh = document.getElementById("refresh-button");
    theme?.addEventListener("click", toggleTheme);
    refresh?.addEventListener("click", () => refreshStatus(true));
    updateThemeButton();
  };

  const tick = () => {
    document.querySelectorAll(".relative-time").forEach((node) => {
      node.textContent = relativeTime(node.getAttribute("datetime"));
      node.title = exactTime(node.getAttribute("datetime"));
    });
    document.querySelectorAll(".live-duration").forEach((node) => {
      const started = parseTime(node.dataset.startedAt);
      if (started) node.textContent = formatDuration((Date.now() - started.getTime()) / 1000);
    });
    document.querySelectorAll(".live-remaining").forEach((node) => {
      const deadlineAt = Number(node.dataset.deadlineAt);
      if (Number.isFinite(deadlineAt)) {
        node.textContent = formatRemaining((deadlineAt - Date.now()) / 1000);
      }
    });
    const countdown = document.getElementById("refresh-countdown");
    if (countdown && nextRefreshAt) {
      const seconds = Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000));
      countdown.textContent = `Refresh in ${seconds}s`;
    }
  };

  const scheduleRefresh = () => {
    clearTimeout(refreshTimer);
    if (document.hidden) return;
    nextRefreshAt = Date.now() + REFRESH_MS;
    refreshTimer = window.setTimeout(() => refreshStatus(false), REFRESH_MS);
  };

  const refreshStatus = async (manual) => {
    if (requestInFlight || document.hidden) return;
    requestInFlight = true;
    clearTimeout(refreshTimer);
    const button = document.getElementById("refresh-button");
    if (button) {
      button.disabled = true;
      button.textContent = "Refreshing…";
    }
    try {
      const response = await fetch("/api/status", { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      snapshot = await response.json();
      updateError = "";
      render();
    } catch (error) {
      updateError = error instanceof Error ? error.message : "Status request failed";
      render();
    } finally {
      requestInFlight = false;
      scheduleRefresh();
      if (manual) tick();
    }
  };

  applyInitialTheme();
  try {
    snapshot = JSON.parse(root.dataset.snapshot || "{}");
  } catch (_error) {
    snapshot = {};
    updateError = "Initial status data was invalid";
  }
  root.removeAttribute("data-snapshot");
  bindTooltipEvents();
  render();
  scheduleRefresh();
  window.setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(refreshTimer);
    } else {
      refreshStatus(false);
    }
  });
})();
