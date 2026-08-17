(() => {
  "use strict";

  const ACTIVE_REFRESH_MS = 3_000;
  const IDLE_REFRESH_MS = 20_000;
  const MAX_BACKOFF_MS = 60_000;
  const RETRY_BATCH_SIZE = 20;
  const UP_NEXT_BATCH_SIZE = 10;
  const ACTIVE_RETRY_STATES = new Set([
    "repair_retry_queued", "regeneration_waiting",
    "regeneration_queued", "retry_in_progress",
  ]);
  const root = document.getElementById("dashboard");
  const configuredTimeZone = root.dataset.timeZone || "UTC";
  let snapshot = {};
  let refreshTimer = null;
  let nextRefreshAt = 0;
  let requestInFlight = false;
  let updateError = "";
  let failureCount = 0;
  let tooltipSequence = 0;
  let activeTooltipTrigger = null;
  let pinnedTooltipTrigger = null;
  let retrySortKey = "nextAction";
  let retrySortDirection = "asc";
  let retryVisibleCount = RETRY_BATCH_SIZE;
  let upNextVisibleCount = UP_NEXT_BATCH_SIZE;
  let queueViewMode = "auto";
  let queueCycleId = null;
  let recoveryDiagnosticsOpen = null;
  let observationSearch = "";
  let observationClassification = "";
  let observationLanguage = "";
  const expandedRetryIds = new Set();
  const expandedObservationIds = new Set();

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
      timeZoneName: "short",
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

  const labelForState = (state) => {
    const clean = String(state || "unknown");
    return {
      waiting_retry: "Waiting for retry",
      series_protected: "Circuit protected",
      missing_source: "Missing source",
      repair_queued: "Repair queued",
      repair_waiting_capacity: "Waiting for capacity",
      repairing: "Repairing",
      repair_validating: "Validating repaired file",
      scanning: "Scanning library",
      waiting_repair_completion: "Waiting for repairs",
      synchronizing: "Synchronizing Bazarr",
      retaining: "Applying retention",
      pruning: "Pruning sidecars",
      startup_wait: "Startup wait",
      startup_sync: "Startup synchronization",
      startup_cleanup: "Startup cleanup",
      cycle_work: "Cycle work",
      retry_recovery: "Retry recovery",
      repair_drain: "Repair drain",
      post_cycle_maintenance: "Post-cycle maintenance",
      cooldown: "Cooldown",
    }[clean] || clean.replaceAll("_", " ");
  };

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

  const operationLabel = (operation) => ({
    translation: "Translation",
    cue_repair: "Cue repair",
    format_repair: "Format repair",
    validation: "Validation",
    quarantine: "Quarantine",
    deletion: "Deletion",
    undersized_detection: "Undersized detection",
    sidecar_pruning: "Sidecar pruning",
    bazarr_sync: "Bazarr synchronization",
    existing_library_scan: "Existing-library scan",
    startup: "Startup",
    retention: "Retention",
  }[operation] || String(operation || "Work").replaceAll("_", " "));

  const progressMarkup = (row) => {
    const percent = Math.max(0, Math.min(100, number(row.progress)));
    const total = number(row.totalRepairableCues);
    const completed = number(row.completedCues);
    const cue = row.currentCueNumber ?? row.currentCuePosition;
    let detail = "";
    if (total) {
      detail = `${completed} of ${total} cues`;
      if (cue !== null && cue !== undefined) detail += `; cue ${cue}`;
    } else if (number(row.filesDiscovered)) {
      detail = `${number(row.filesChecked)} of ${number(row.filesDiscovered)} files`;
    }
    const attempt = row.currentAttempt
      ? `Attempt ${number(row.currentAttempt)} of ${number(row.maxAttempts) || "n/a"}`
      : "";
    const stageLabels = {
      waiting_capacity: "Waiting for capacity",
      starting: "Starting repair",
      calling_lingarr: "Calling Lingarr",
      validating_candidate: "Validating returned cue",
      repairing: "Repairing cues",
      repair_validating: "Validating completed file",
      queued: "Queued",
    };
    const stage = stageLabels[row.repairStage] || "";
    if (!detail && !attempt && !stage) return '<span class="duration">n/a</span>';
    return `<div class="job-progress"><span>${escapeHtml([stage, detail, attempt].filter(Boolean).join(" / "))}</span>`
      + `<progress max="100" value="${percent}" aria-label="${escapeHtml(`${operationLabel(row.operation)} progress`)}">${percent}%</progress></div>`;
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
      detail.category ? `Category: ${detail.category}` : null,
      detail.provider && detail.provider !== "unknown" ? `Provider: ${detail.provider}` : null,
      detail.model && detail.model !== "unknown" ? `Model: ${detail.model}` : null,
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
        ["Work", "work"],
        ["Media", "media"],
        ...(showType ? [["Type", "type"]] : []),
        ["Language", "language"],
        ["Status", "status"],
        ["Operation", "operation"],
        ["Progress", "progress"],
        ["Elapsed", "elapsed"],
        ["Est. total", "estimate"],
        ["Remaining", "eta"],
        ["Started", "started"],
      ];
    } else {
      columns = [
        ["Work", "work"],
        ["Media", "media"],
        ...(showType ? [["Type", "type"]] : []),
        ["Language", "language"],
        ["Operation", "operation"],
        ["Outcome", "outcome"],
        ["Duration", "duration"],
        ["Attempts", "attempts"],
        ["Finished", "finished"],
      ];
    }

    const cell = (row, key, index) => {
      if (key === "position") return `<span class="queue-position">#${index + 1}</span>`;
      if (key === "work") {
        const maintenance = row.workKind === "maintenance";
        const label = row.operation === "startup" ? "Startup" : maintenance ? "Maintenance" : "Cycle";
        return `<span class="badge ${maintenance ? "maintenance-work" : "cycle-work"}">${escapeHtml(label)}</span>`;
      }
      if (key === "media") return mediaMarkup(row);
      if (key === "type") return escapeHtml(row.itemType === "movies" ? "Movie" : "Episode");
      if (key === "operation") return escapeHtml(operationLabel(row.operation));
      if (key === "progress") return progressMarkup(row);
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

  const panelHeader = (title, note = "", actions = "") => (
    `<div class="panel-header"><div><h2>${escapeHtml(title)}</h2>`
    + (note ? `<p class="section-note">${note}</p>` : "")
    + `</div>${actions}</div>`
  );

  const observationClassificationLabel = (value) => ({
    likely_invariant: "Likely invariant",
    ambiguous: "Ambiguous",
  }[value] || labelForState(value));

  const observationId = (row) => [
    row.itemType, row.itemId, row.targetLanguage, row.cueNumber,
    row.classification, row.timestamp,
  ].map((value) => String(value ?? "")).join(":");

  const observationEvidence = (evidence = {}) => {
    const confidence = (value) => (
      value === null || value === undefined ? "n/a" : Number(value).toFixed(3)
    );
    const rows = [
      ["Similarity", confidence(evidence.similarity)],
      ["Exact normalized copy", evidence.exactNormalizedCopy ? "Yes" : "No"],
      ["Token count", evidence.tokenCount ?? "n/a"],
      ["Token shape", labelForState(evidence.tokenShape)],
      ["Model markers", evidence.modelMarkerCount ?? 0],
      ["Cue language", evidence.cueLanguage || "Unknown"],
      ["Cue language confidence", confidence(evidence.cueLanguageConfidence)],
      ["Whole-target confidence", confidence(evidence.wholeTargetConfidence)],
      ["Context confidence", confidence(evidence.contextConfidence)],
    ];
    return `<dl class="observation-evidence">${rows.map(([label, value]) => (
      `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
    )).join("")}</dl>`;
  };

  const renderValidationObservations = (observations) => {
    const rows = Array.isArray(observations) ? observations : [];
    const languages = [...new Set(rows.map((row) => row.targetLanguage).filter(Boolean))].sort();
    const search = observationSearch.trim().toLocaleLowerCase();
    const filtered = rows.filter((row) => {
      if (observationClassification && row.classification !== observationClassification) return false;
      if (observationLanguage && row.targetLanguage !== observationLanguage) return false;
      if (!search) return true;
      return [
        row.title, row.episodeCode, row.episodeTitle, row.itemType,
        row.targetLanguage, row.classification, row.reason, row.cueNumber,
      ].filter((value) => value !== null && value !== undefined)
        .join(" ").toLocaleLowerCase().includes(search);
    });
    const filters = `<form class="observation-filters" id="observation-filters">
      <label>Search <input type="search" maxlength="100" value="${escapeHtml(observationSearch)}"
        placeholder="Media, cue, or decision" data-observation-focus="search" data-focus-key="observation-search"></label>
      <label>Classification <select data-observation-focus="classification" data-focus-key="observation-classification">
        <option value="">All classifications</option>
        ${["likely_invariant", "ambiguous"].map((value) => (
          `<option value="${value}"${observationClassification === value ? " selected" : ""}>${escapeHtml(observationClassificationLabel(value))}</option>`
        )).join("")}
      </select></label>
      <label>Language <select data-observation-focus="language" data-focus-key="observation-language">
        <option value="">All languages</option>
        ${languages.map((value) => (
          `<option value="${escapeHtml(value)}"${observationLanguage === value ? " selected" : ""}>${escapeHtml(value)}</option>`
        )).join("")}
      </select></label>
    </form>`;
    let body;
    if (!rows.length) {
      body = '<p class="empty-state">No copied-source repairs were suppressed.</p>';
    } else if (!filtered.length) {
      body = '<p class="empty-state">No observations match these filters.</p>';
    } else {
      body = `<div class="table-wrap"><table class="data-table observation-table">
        <thead><tr><th scope="col">Media</th><th scope="col">Type</th><th scope="col">Language</th>
        <th scope="col">Cue</th><th scope="col">Decision</th><th scope="col">Classification</th>
        <th scope="col">Evidence</th><th scope="col">Observed</th></tr></thead>
        <tbody>${filtered.map((row) => {
          const id = observationId(row);
          const open = expandedObservationIds.has(id) ? " open" : "";
          return `<tr>
          <td class="cell-media" data-label="Media">${mediaMarkup(row)}</td>
          <td data-label="Type">${escapeHtml(row.itemType === "movies" ? "Movie" : row.itemType === "episodes" ? "Episode" : "—")}</td>
          <td data-label="Language">${escapeHtml(row.targetLanguage || "—")}</td>
          <td data-label="Cue">${escapeHtml(row.cueNumber ?? "—")}</td>
          <td data-label="Decision"><span class="badge badge-warning">Repair skipped</span></td>
          <td data-label="Classification">${escapeHtml(observationClassificationLabel(row.classification))}</td>
          <td data-label="Evidence"><details class="observation-details" data-observation-id="${escapeHtml(id)}"${open}><summary data-focus-key="observation-${escapeHtml(id)}">View evidence</summary>
            <p>${escapeHtml(row.reason || "Copied-source repair was suppressed.")}</p>
            ${observationEvidence(row.evidence)}</details></td>
          <td data-label="Observed">${timeMarkup(row.timestamp)}</td>
        </tr>`;
        }).join("")}</tbody></table></div>`;
    }
    return `<section class="panel observation-panel">${panelHeader("Validation observations", "Latest 20 suppressed copied-source decisions")}${filters}${body}</section>`;
  };

  const renderHeader = (service, cycle, manualReviewCount) => {
    const phase = labelForState(service.phase || "startup");
    const generated = parseTime(snapshot.generatedAt);
    const stale = !generated || Date.now() - generated.getTime() > 30_000;
    const error = updateError || stale
      ? `<span class="status-warning" role="status">Update delayed</span>`
      : '<span id="refresh-countdown">Refresh scheduled</span>';
    return `<header class="topbar">
      <div>
        <div class="eyebrow"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(phase)}</div>
        <h1>Translation status</h1>
        <p class="header-meta">
          <span>Cycle #${escapeHtml(cycle.number ?? "—")}</span>
          <span id="freshness">Last updated ${timeMarkup(snapshot.generatedAt)}</span>
          <span>${error}</span>
        </p>
      </div>
      <div class="header-actions">
        <a class="btn btn-secondary" href="/" aria-current="page" data-focus-key="nav-status">Status</a>
        <a class="btn btn-secondary" href="/review" data-focus-key="nav-review">Manual review (${number(manualReviewCount).toLocaleString()})</a>
        <a class="btn btn-secondary" href="/logs" data-focus-key="nav-logs">Logs</a>
        <button class="btn btn-secondary" id="theme-toggle" type="button" aria-label="Switch color theme" data-focus-key="theme-toggle">Theme</button>
        <button class="btn btn-primary" id="refresh-button" type="button" data-focus-key="refresh-button">Refresh now</button>
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
              ${metric("Waiting for retry", number(cycle.waitingRetry).toLocaleString(), "tone-warning")}
              ${metric("Circuit protected", number(cycle.seriesProtected).toLocaleString(), "tone-warning")}
              ${metric("Missing source", number(cycle.missingSource).toLocaleString(), "tone-warning")}
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
      (entry) => (entry.state === "open" || entry.state === "half_open" || entry.state === "eligible")
        && entry.seriesTitle,
    );
    const breakerRows = activeCircuits.map((entry) => {
      const failures = number(entry.failures);
      const eligible = Number(entry.eligibleAfterCycle);
      const remaining = Math.max(0, number(entry.completedCyclesRemaining));
      let trial = "Trial ready";
      if (entry.state === "half_open" && entry.trialState === "validation_pending") {
        trial = "Trial awaiting repair/validation";
      } else if (entry.state === "half_open" && entry.trialJobId != null) {
        trial = "Trial in progress";
      } else if (remaining > 0) {
        trial = `Trial in ${remaining.toLocaleString()} ${remaining === 1 ? "cycle" : "cycles"}`;
      } else if (!Number.isFinite(eligible) && entry.state !== "half_open") {
        return "";
      }
      return `<div class="protection-series">
        <strong>${escapeHtml(entry.seriesTitle)}</strong>
        <span>${failures.toLocaleString()} consecutive ${failures === 1 ? "failure" : "failures"} - ${trial}</span>
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

  const renderRecoveryDiagnostics = (diagnostics) => {
    const donors = diagnostics?.donors || {};
    const repairs = diagnostics?.repairs || {};
    const admissions = diagnostics?.retryAdmissions || {};
    const provider = diagnostics?.providerHealth || {};
    const maintenance = diagnostics?.maintenance || null;
    const rows = [
      ["Donor candidates selected", donors.selected],
      ["Donors rejected by validation", donors.current_validation_failed],
      ["Retry plans examined", admissions.examined],
      ["Retry submissions", admissions.submitted],
      ["No-progress admissions", admissions.no_progress],
      ["Queued repair jobs", repairs.queued],
      ["Restart-persisted repairs", repairs.persisted_for_restart],
      ["Malformed provider responses", provider.malformed_response],
    ];
    const hasActivity = rows.some(([, value]) => number(value) > 0) || Boolean(maintenance);
    const cards = rows.map(([label, value]) => (
      `<div class="maintenance-item"><span class="maintenance-label">${escapeHtml(label)}</span>`
      + `<strong class="maintenance-value">${number(value).toLocaleString()}</strong></div>`
    )).join("");
    const maintenanceNote = maintenance
      ? `Latest maintenance: ${operationLabel(maintenance.operation)} - ${labelForState(maintenance.state)}`
      : "No persisted maintenance run yet.";
    const open = recoveryDiagnosticsOpen ?? hasActivity;
    return `<details class="panel diagnostics-panel" data-recovery-diagnostics${open ? " open" : ""}>`
      + `<summary data-focus-key="recovery-diagnostics"><span>Recovery reliability</span><small>${escapeHtml(maintenanceNote)}</small></summary>`
      + `<div class="maintenance-grid">${cards}</div></details>`;
  };

  const renderRecoveryAttention = (plans, completedCycle, manualReviewCount) => {
    const dueNow = plans.filter((plan) => (
      plan.state === "regeneration_waiting"
      && number(plan.eligibleCompletedCycle) <= number(completedCycle)
    )).length;
    const dueNext = plans.filter((plan) => (
      plan.state === "regeneration_waiting"
      && number(plan.eligibleCompletedCycle) === number(completedCycle) + 1
    )).length;
    return `<nav class="recovery-attention" aria-label="Recovery attention">
      <a href="#retry-queue" data-open-retry-queue><span>Due now</span><strong>${dueNow.toLocaleString()}</strong></a>
      <a href="#retry-queue" data-open-retry-queue><span>Next cycle</span><strong>${dueNext.toLocaleString()}</strong></a>
      <a href="/review"><span>Manual review</span><strong>${number(manualReviewCount).toLocaleString()}</strong></a>
    </nav>`;
  };

  const activeRetryPlans = (plans) => plans.filter((plan) => (
    !plan.manualReview && ACTIVE_RETRY_STATES.has(plan.state)
  ));

  const retryMedia = (plan) => ({
    title: plan.displayTitle || `${plan.itemType || "media"} ${plan.itemId ?? "?"}`,
    detail: [plan.episodeCode, plan.episodeTitle].filter(Boolean).join(" - "),
  });

  const retryPlanId = (plan) => String(
    plan.id ?? `${plan.itemType || "media"}-${plan.itemId ?? "unknown"}-${plan.targetLanguage || "unknown"}`,
  );

  const retryDetailId = (plan) => (
    `retry-detail-${retryPlanId(plan).replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`
  );

  const retryState = (state, plan = null, completedCycle = 0) => {
    const states = {
      regeneration_waiting: [
        plan && number(plan.eligibleCompletedCycle) > number(completedCycle)
          ? (number(plan.eligibleCompletedCycle) === number(completedCycle) + 1 ? "Next cycle" : "Scheduled")
          : "Due now",
        "deferred",
      ],
      regeneration_queued: ["Admitted", "translating"],
      waiting_lane: ["Waiting for lane", "queued"],
      retry_in_progress: ["Translating", "translating"],
      repair_retry_queued: ["Repair queued", "repairing"],
      retry_exhausted: ["Retry exhausted", "failed"],
      source_blocked: ["Source blocked", "failed"],
    };
    return states[state] || [labelForState(state || "queued"), "queued"];
  };

  const retryNextAction = (plan, completedCycle) => {
    if (plan.manualReview) return "Manual review";
    const reason = String(plan.lastReason || "").toLowerCase();
    if (reason.includes("circuit")) return "Waiting for circuit";
    if (plan.runtimeState === "waiting_lane") return "Waiting for lane";
    if (plan.runtimeState === "retry_in_progress") return "Translating";
    if (plan.state === "regeneration_queued") return "Admitted";
    if (plan.state === "retry_in_progress") return "Translating";
    if (plan.state === "regeneration_waiting") {
      const cyclesRemaining = Math.max(
        0,
        number(plan.eligibleCompletedCycle) - number(completedCycle),
      );
      if (cyclesRemaining === 0) return "Due now";
      if (plan.lastDeferralClass) return "Rescheduled after no progress";
      if (cyclesRemaining === 1) return "Next cycle";
      return `In ${cyclesRemaining} Cycles`;
    }
    if (plan.state === "repair_retry_queued") return "Repair at cycle end";
    if (plan.state === "retry_exhausted" || plan.state === "source_blocked") {
      return "Manual review";
    }
    return labelForState(plan.state || "queued");
  };

  const retryActionOrder = (plan, completedCycle) => {
    if ([
      "regeneration_queued", "retry_in_progress", "repair_retry_queued",
    ].includes(plan.state)) {
      return [0, 0];
    }
    if (plan.state === "regeneration_waiting") {
      const cyclesRemaining = Math.max(
        0,
        number(plan.eligibleCompletedCycle) - number(completedCycle),
      );
      return cyclesRemaining === 0 ? [0, 1] : [1, cyclesRemaining];
    }
    const reason = String(plan.lastReason || "").toLowerCase();
    if (reason.includes("circuit")) return [2, 0];
    if (plan.state === "retry_exhausted" || plan.state === "source_blocked") {
      return [3, 0];
    }
    return [2, String(retryNextAction(plan, completedCycle)).toLocaleLowerCase()];
  };

  const retrySortValue = (plan, key, completedCycle) => {
    const [stateLabel] = retryState(plan.state, plan, completedCycle);
    if (key === "media") return retryMedia(plan).title.toLocaleLowerCase();
    if (key === "language") return String(plan.targetLanguage || "").toLocaleLowerCase();
    if (key === "status") return stateLabel.toLocaleLowerCase();
    if (key === "attempts") return number(plan.attemptCount);
    return retryActionOrder(plan, completedCycle);
  };

  const compareRetryValues = (left, right) => {
    if (Array.isArray(left) && Array.isArray(right)) {
      for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
        const comparison = compareRetryValues(left[index], right[index]);
        if (comparison) return comparison;
      }
      return 0;
    }
    if (typeof left === "number" && typeof right === "number") return left - right;
    return String(left ?? "").localeCompare(String(right ?? ""), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  };

  const compareRetryPlans = (left, right, completedCycle) => {
    const primary = compareRetryValues(
      retrySortValue(left, retrySortKey, completedCycle),
      retrySortValue(right, retrySortKey, completedCycle),
    );
    if (primary) return retrySortDirection === "desc" ? -primary : primary;
    const titleOrder = compareRetryValues(retryMedia(left).title, retryMedia(right).title);
    if (titleOrder) return titleOrder;
    return compareRetryValues(retryPlanId(left), retryPlanId(right));
  };

  const retryCodeLabels = {
    whole_file_validation_failure: "Whole-file validation failed",
    copied_source: "Copied source text",
    manual_review: "Manual review",
    no_progress: "No progress",
    accepted_after_retry: "Accepted after retry",
    accepted_after_manual_recheck: "Accepted after manual recheck",
  };
  const retryCodeLabel = (value) => retryCodeLabels[String(value || "")]
    || String(value || "").replaceAll("_", " ");

  const retryDetails = (plan, expanded) => {
    const rules = Array.isArray(plan.rules) && plan.rules.length
      ? plan.rules.map(retryCodeLabel).join(", ")
      : "—";
    const fields = [
      ["Failure class", plan.failureClass || "—"],
      ["Validation rules", rules],
      ["Last reason", plan.lastReason || "—"],
      ["Eligible cycle", plan.eligibleCompletedCycle ?? "—"],
      ["Last admitted cycle", plan.lastAdmittedCycle ?? "—"],
      ["No-progress count", plan.noProgressCount ?? 0],
      ["Last deferral", plan.lastDeferralClass || "—"],
      ["Archived attempts", plan.archivedAttemptCount ?? 0],
      ["Latest donor attempt", plan.latestDonorAttempt ?? "—"],
      ["Item", `${plan.itemType || "media"}:${plan.itemId ?? "—"}`],
      ["Final outcome", plan.finalOutcome || "—"],
    ];
    const detailId = retryDetailId(plan);
    return `<tr class="retry-detail-row" id="${escapeHtml(detailId)}" ${expanded ? "" : "hidden"}>
      <td colspan="6">
        <dl class="retry-detail-grid">${fields.map(([label, value]) => (
          `<div class="${label === "Last reason" ? "retry-detail-wide" : ""}"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(retryCodeLabel(value))}</dd></div>`
        )).join("")}</dl>
      </td>
    </tr>`;
  };

  const retrySortLabels = {
    media: "Media",
    language: "Language",
    status: "Status",
    attempts: "Retries",
    nextAction: "Next action",
  };

  const retrySortHeader = (key) => {
    const active = retrySortKey === key;
    const ariaSort = active
      ? ` aria-sort="${retrySortDirection === "asc" ? "ascending" : "descending"}"`
      : "";
    const indicator = active ? (retrySortDirection === "asc" ? "↑" : "↓") : "↕";
    return `<th scope="col"${ariaSort}>
      <button type="button" class="retry-sort-button ${active ? "is-active" : ""}"
        data-retry-sort="${escapeHtml(key)}" data-retry-focus="sort-${escapeHtml(key)}">
        ${escapeHtml(retrySortLabels[key])}<span aria-hidden="true">${indicator}</span>
      </button>
    </th>`;
  };

  const renderRetryPlans = (plans, completedCycle, maxAttempts, cycleJobs = []) => {
    const jobsByPlan = new Map(
      cycleJobs.filter((job) => job.retryPlanId != null).map(
        (job) => [String(job.retryPlanId), job],
      ),
    );
    const active = activeRetryPlans(plans).map((plan) => {
      const job = jobsByPlan.get(retryPlanId(plan));
      if (!job) return plan;
      const runtimeState = ["translating", "validating"].includes(job.state)
        ? "retry_in_progress"
        : "waiting_lane";
      return { ...plan, runtimeState };
    });
    const activeIds = new Set(active.map(retryPlanId));
    expandedRetryIds.forEach((id) => {
      if (!activeIds.has(id)) expandedRetryIds.delete(id);
    });
    if (!active.length) {
      return '<p class="empty-state">No retry work scheduled.</p>';
    }
    const sorted = [...active].sort((left, right) => (
      compareRetryPlans(left, right, completedCycle)
    ));
    const visible = sorted.slice(0, retryVisibleCount);
    const dueNow = active.filter((plan) => (
      plan.state === "regeneration_waiting"
      && number(plan.eligibleCompletedCycle) <= number(completedCycle)
    )).length;
    const dueNext = active.filter((plan) => (
      plan.state === "regeneration_waiting"
      && number(plan.eligibleCompletedCycle) === number(completedCycle) + 1
    )).length;
    const rows = visible.map((plan) => {
      const media = retryMedia(plan);
      const [stateLabel, stateTone] = retryState(plan.runtimeState || plan.state, plan, completedCycle);
      const planId = retryPlanId(plan);
      const detailId = retryDetailId(plan);
      const expanded = expandedRetryIds.has(planId);
      return `<tr class="retry-main-row ${expanded ? "has-expanded" : ""}">
        <td class="cell-media" data-label="Media">
          <span class="media-title">${escapeHtml(media.title)}</span>
          ${media.detail ? `<span class="media-detail">${escapeHtml(media.detail)}</span>` : ""}
        </td>
        <td data-label="Language">${escapeHtml(plan.targetLanguage || "—")}</td>
        <td data-label="Status"><span class="badge ${stateTone}">${escapeHtml(stateLabel)}</span></td>
        <td data-label="Retries"><span class="duration">${Number(maxAttempts) === 0
          ? `${Number(plan.attemptCount || 0)} retries &middot; Unlimited`
          : `${Number(plan.attemptCount || 0)} of ${Number(maxAttempts)} used`}</span></td>
        <td data-label="Next action">${escapeHtml(retryNextAction(plan, completedCycle))}</td>
        <td class="cell-details" data-label="Details">
          <button type="button" class="retry-details-toggle"
            data-retry-id="${escapeHtml(planId)}" data-retry-focus="details-${escapeHtml(planId)}"
            aria-expanded="${expanded ? "true" : "false"}" aria-controls="${escapeHtml(detailId)}">
            <span class="retry-details-icon" aria-hidden="true"></span>
            <span class="retry-details-label">${expanded ? "Hide details" : "View details"}</span>
          </button>
        </td>
      </tr>${retryDetails(plan, expanded)}`;
    }).join("");
    const sortOptions = Object.entries(retrySortLabels).map(([key, label]) => (
      `<option value="${escapeHtml(key)}" ${retrySortKey === key ? "selected" : ""}>${escapeHtml(label)}</option>`
    )).join("");
    const remaining = Math.max(0, active.length - visible.length);
    return `<div class="retry-toolbar">
        <div class="retry-summary" aria-label="${active.length} active retries, ${dueNow} due now, ${dueNext} due next cycle">
          <span><strong>${active.length.toLocaleString()}</strong> active</span>
          <span><strong>${dueNow.toLocaleString()}</strong> due now</span>
          <span><strong>${dueNext.toLocaleString()}</strong> next cycle</span>
        </div>
        <div class="retry-mobile-sort">
          <label for="retry-sort-select">Sort by</label>
          <select id="retry-sort-select" data-retry-focus="sort-select">${sortOptions}</select>
          <button type="button" class="retry-sort-direction" data-retry-focus="sort-direction"
            aria-label="Sort ${retrySortDirection === "asc" ? "descending" : "ascending"}">
            <span aria-hidden="true">${retrySortDirection === "asc" ? "↑" : "↓"}</span>
          </button>
        </div>
      </div>
      <div class="table-wrap"><table class="data-table retry-table">
        <thead><tr>${retrySortHeader("media")}${retrySortHeader("language")}${retrySortHeader("status")}${retrySortHeader("attempts")}${retrySortHeader("nextAction")}<th scope="col">Details</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="retry-table-footer">
        <span class="retry-showing" aria-live="polite">Showing ${visible.length.toLocaleString()} of ${active.length.toLocaleString()}</span>
        ${remaining ? `<button type="button" class="btn btn-secondary btn-sm retry-show-more" data-retry-focus="show-more">Show ${Math.min(RETRY_BATCH_SIZE, remaining).toLocaleString()} more</button>` : ""}
      </div></div>`;
  };

  const queueViewControl = () => {
    const options = [
      ["auto", "Auto"], ["up-next", "Up next"], ["retry", "Retry queue"],
    ];
    return `<div class="queue-view-switch" role="group" aria-label="Queue view">${options.map(([value, label]) => (
      `<button type="button" data-queue-mode="${value}" data-queue-focus="mode-${value}"
        aria-pressed="${queueViewMode === value ? "true" : "false"}">${label}</button>`
    )).join("")}</div>`;
  };

  const automaticQueueView = (service, activeRetries) => {
    if (service.phase === "translating") return "up-next";
    if (["retry_recovery", "repair_drain"].includes(service.phase)) {
      return activeRetries.length ? "retry" : "up-next";
    }
    return activeRetries.length ? "retry" : "up-next";
  };

  const renderUpNext = (upcoming) => {
    const visible = upcoming.slice(0, upNextVisibleCount);
    const remaining = Math.max(0, upcoming.length - visible.length);
    return `${table(visible, "upcoming", "No queued jobs.")}
      <div class="queue-table-footer">
        <span class="queue-showing" aria-live="polite">Showing ${visible.length.toLocaleString()} of ${upcoming.length.toLocaleString()}</span>
        ${remaining ? `<button type="button" class="btn btn-secondary btn-sm up-next-show-more"
          data-queue-focus="up-next-more">Show ${Math.min(UP_NEXT_BATCH_SIZE, remaining).toLocaleString()} more</button>` : ""}
      </div>`;
  };

  const renderCombinedQueue = (service, upcoming, retryPlans, completedCycle, maxAttempts, cycleJobs) => {
    const activeRetries = activeRetryPlans(retryPlans);
    const visibleView = queueViewMode === "auto"
      ? automaticQueueView(service, activeRetries)
      : queueViewMode;
    const retryVisible = visibleView === "retry";
    const title = retryVisible ? "Retry queue" : "Up next";
    const note = retryVisible
      ? `${activeRetries.length.toLocaleString()} active · persistent quarantine recovery · completed cycle ${Number(completedCycle || 0)}`
      : `${upcoming.length.toLocaleString()} queued job${upcoming.length === 1 ? "" : "s"}`;
    const content = retryVisible
      ? renderRetryPlans(retryPlans, completedCycle, maxAttempts, cycleJobs)
      : renderUpNext(upcoming);
    return `<section class="panel combined-queue" id="retry-queue" data-queue-view="${visibleView}">
      ${panelHeader(title, note, queueViewControl())}${content}
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
        ${line("Waiting for retry", "waiting_retry", "tone-warning")}
        ${line("Circuit protected", "series_protected", "tone-warning")}
        ${line("Missing source", "missing_source", "tone-warning")}
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
    cycle_suppressions: "Same-cycle suppressions",
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

  const renderMaintenanceRolling = (history) => {
    const cards = Object.entries(history || {}).map(([window, values]) => (
      `<article class="window-card"><h3 class="window-title">${escapeHtml(window)}</h3>`
      + `<div class="maintenance-window">${Object.entries(maintenanceLabels).map(([key, label]) => (
        `<div class="outcome-line ${key === "failures" ? "tone-danger" : ""} ${number(values?.[key]) ? "" : "is-zero"}">`
        + `<span>${escapeHtml(label)}</span><strong>${number(values?.[key]).toLocaleString()}</strong></div>`
      )).join("")}</div></article>`
    )).join("");
    return `<section class="panel">${panelHeader("Rolling maintenance", "Maintenance totals are separate from wanted-cycle outcomes.")}
      <div class="rolling-grid">${cards}</div></section>`;
  };

  const render = () => {
    const stableFocusKey = document.activeElement?.dataset?.focusKey || "";
    const retryFocusKey = document.activeElement?.dataset?.retryFocus || "";
    const queueFocusKey = document.activeElement?.dataset?.queueFocus || "";
    const observationFocusKey = document.activeElement?.dataset?.observationFocus || "";
    const observationSelectionStart = document.activeElement?.selectionStart;
    const service = snapshot.service || {};
    const cycle = snapshot.currentCycle || {};
    const maintenance = snapshot.maintenance || {};
    const active = [
      ...(snapshot.activeJobs || []),
      ...(maintenance.activeJobs || []),
    ];
    const upcoming = snapshot.upNext || [];
    const recent = snapshot.recentOutcomes || [];
    activeTooltipTrigger = null;
    pinnedTooltipTrigger = null;
    tooltipSequence = 0;
    const retryPlans = snapshot.retryPlans || [];
    const manualReviewPlans = retryPlans.filter((plan) => plan.manualReview);
    const automaticRetryPlans = activeRetryPlans(retryPlans);
    const nextQueueCycleId = cycle.id ?? null;
    if (queueCycleId !== null && nextQueueCycleId !== queueCycleId) {
      upNextVisibleCount = UP_NEXT_BATCH_SIZE;
    }
    queueCycleId = nextQueueCycleId;
    root.innerHTML = `<div class="dashboard-shell">
      ${renderHeader(service, cycle, manualReviewPlans.length)}
      ${renderOverview(cycle, service)}
      <section class="panel">${panelHeader("Active now", `${active.length.toLocaleString()} in progress`)}
        ${table(active, "active", "No active translations, repairs, startup, or maintenance.")}
      </section>
      ${renderRecoveryAttention(automaticRetryPlans, snapshot.completedCycle || 0, manualReviewPlans.length)}
      ${renderCombinedQueue(
        service,
        upcoming,
        retryPlans,
        snapshot.completedCycle || 0,
        snapshot.retryMaxAttempts ?? 0,
        [...active, ...upcoming],
      )}
      ${renderDiagnostics(snapshot.timing || {}, snapshot.circuits || [])}
      ${renderRecoveryDiagnostics(service.recoveryDiagnostics || {})}
      <section class="panel">${panelHeader("Recent outcomes", "Latest completed work")}
        ${table(recent, "recent", "No completed jobs recorded yet.")}
      </section>
      ${renderValidationObservations(snapshot.validationObservations || [])}
      <section class="panel">${panelHeader("Recent maintenance", "Latest completed maintenance work")}
        ${table(maintenance.recentOutcomes || [], "recent", "No maintenance outcomes recorded yet.")}
      </section>
      ${renderRolling(snapshot.history || {})}
      ${renderMaintenanceRolling(maintenance.history || {})}
      ${renderMaintenance(maintenance)}
      <p class="footer-note">Auto-refreshes every 3 seconds while active and 20 seconds while idle · trusted LAN endpoint · no subtitle text or filesystem paths exposed</p>
    </div>`;
    root.setAttribute("aria-busy", "false");
    bindControls();
    if (stableFocusKey) {
      const stableFocusTarget = root.querySelector(
        `[data-focus-key="${CSS.escape(stableFocusKey)}"]`,
      );
      stableFocusTarget?.focus({ preventScroll: true });
      if (
        typeof observationSelectionStart === "number"
        && typeof stableFocusTarget?.setSelectionRange === "function"
      ) {
        stableFocusTarget.setSelectionRange(
          observationSelectionStart, observationSelectionStart,
        );
      }
    }
    if (retryFocusKey) {
      const retryFocusTarget = Array.from(
        root.querySelectorAll("[data-retry-focus]"),
      ).find((node) => node.dataset.retryFocus === retryFocusKey);
      retryFocusTarget?.focus({ preventScroll: true });
    }
    if (queueFocusKey) {
      const queueFocusTarget = Array.from(
        root.querySelectorAll("[data-queue-focus]"),
      ).find((node) => node.dataset.queueFocus === queueFocusKey);
      queueFocusTarget?.focus({ preventScroll: true });
    }
    if (observationFocusKey) {
      const observationFocusTarget = root.querySelector(
        `[data-observation-focus="${observationFocusKey}"]`,
      );
      observationFocusTarget?.focus({ preventScroll: true });
      if (
        typeof observationSelectionStart === "number"
        && typeof observationFocusTarget?.setSelectionRange === "function"
      ) {
        observationFocusTarget.setSelectionRange(
          observationSelectionStart, observationSelectionStart,
        );
      }
    }
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

  const bindRetryControls = () => {
    document.querySelectorAll("[data-retry-sort]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.retrySort;
        if (!Object.hasOwn(retrySortLabels, key)) return;
        if (retrySortKey === key) {
          retrySortDirection = retrySortDirection === "asc" ? "desc" : "asc";
        } else {
          retrySortKey = key;
          retrySortDirection = "asc";
        }
        retryVisibleCount = RETRY_BATCH_SIZE;
        render();
      });
    });

    const select = document.getElementById("retry-sort-select");
    select?.addEventListener("change", () => {
      if (!Object.hasOwn(retrySortLabels, select.value)) return;
      retrySortKey = select.value;
      retrySortDirection = "asc";
      retryVisibleCount = RETRY_BATCH_SIZE;
      render();
    });

    document.querySelector(".retry-sort-direction")?.addEventListener("click", () => {
      retrySortDirection = retrySortDirection === "asc" ? "desc" : "asc";
      retryVisibleCount = RETRY_BATCH_SIZE;
      render();
    });

    document.querySelectorAll(".retry-details-toggle").forEach((button) => {
      button.addEventListener("click", () => {
        const planId = button.dataset.retryId;
        const detailId = button.getAttribute("aria-controls");
        const detailRow = detailId ? document.getElementById(detailId) : null;
        if (!planId || !detailRow) return;
        const expanded = button.getAttribute("aria-expanded") !== "true";
        if (expanded) {
          expandedRetryIds.add(planId);
        } else {
          expandedRetryIds.delete(planId);
        }
        button.setAttribute("aria-expanded", String(expanded));
        button.querySelector(".retry-details-label").textContent = (
          expanded ? "Hide details" : "View details"
        );
        button.closest(".retry-main-row")?.classList.toggle("has-expanded", expanded);
        detailRow.hidden = !expanded;
      });
    });

    document.querySelector(".retry-show-more")?.addEventListener("click", () => {
      retryVisibleCount += RETRY_BATCH_SIZE;
      render();
    });
  };

  const bindQueueControls = () => {
    document.querySelectorAll("[data-queue-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!["auto", "up-next", "retry"].includes(button.dataset.queueMode)) return;
        queueViewMode = button.dataset.queueMode;
        render();
      });
    });
    document.querySelector(".up-next-show-more")?.addEventListener("click", () => {
      upNextVisibleCount += UP_NEXT_BATCH_SIZE;
      render();
    });
    document.querySelectorAll("[data-open-retry-queue]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        queueViewMode = "retry";
        render();
        document.getElementById("retry-queue")?.scrollIntoView({ block: "start" });
        document.querySelector('[data-queue-mode="retry"]')?.focus({ preventScroll: true });
      });
    });
  };

  const bindControls = () => {
    const theme = document.getElementById("theme-toggle");
    const refresh = document.getElementById("refresh-button");
    theme?.addEventListener("click", toggleTheme);
    refresh?.addEventListener("click", () => refreshStatus(true));
    bindRetryControls();
    bindQueueControls();
    const observationFilters = document.getElementById("observation-filters");
    const observationSearchInput = observationFilters?.querySelector('input[type="search"]');
    const observationSelects = observationFilters?.querySelectorAll("select") || [];
    observationSearchInput?.addEventListener("input", () => {
      observationSearch = observationSearchInput.value;
      render();
    });
    observationSelects[0]?.addEventListener("change", () => {
      observationClassification = observationSelects[0].value;
      render();
    });
    observationSelects[1]?.addEventListener("change", () => {
      observationLanguage = observationSelects[1].value;
      render();
    });
    document.querySelectorAll("[data-observation-id]").forEach((disclosure) => {
      disclosure.addEventListener("toggle", () => {
        const id = disclosure.dataset.observationId;
        if (disclosure.open) expandedObservationIds.add(id);
        else expandedObservationIds.delete(id);
      });
    });
    document.querySelector("[data-recovery-diagnostics]")?.addEventListener("toggle", (event) => {
      recoveryDiagnosticsOpen = event.currentTarget.open;
    });
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
    const active = (snapshot.activeJobs?.length || 0)
      + (snapshot.maintenance?.activeJobs?.length || 0);
    const baseDelay = active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS;
    const delay = failureCount
      ? Math.min(MAX_BACKOFF_MS, baseDelay * (2 ** failureCount))
      : baseDelay;
    nextRefreshAt = Date.now() + delay;
    refreshTimer = window.setTimeout(() => refreshStatus(false), delay);
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
      failureCount = 0;
      render();
    } catch (error) {
      updateError = error instanceof Error ? error.message : "Status request failed";
      failureCount += 1;
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
