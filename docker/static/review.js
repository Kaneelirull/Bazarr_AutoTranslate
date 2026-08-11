(() => {
  "use strict";

  const root = document.getElementById("manual-review");
  const configuredTimeZone = root.dataset.timeZone || "UTC";
  let payload = null;
  let loading = false;
  let viewState = "loading";
  let loadError = "";
  let actionMessage = "";
  let actionError = false;
  let savedFocus = null;
  let actionFocus = null;
  let filterState = { page: "1", pageSize: "20", sort: "updatedAt", direction: "desc" };
  const expandedReviewIds = new Set();
  const operatorLabels = {
    episodes: "Episode", movies: "Movie",
    whole_file_validation_failure: "Whole-file validation failed",
    copied_source: "Copied source text",
    target_unavailable: "Target file unavailable",
    target_unavailable_after_external_restoration_check: "Target file was not restored",
    validation_passed: "Validation passed",
    valid_with_warnings: "Valid with observations",
    "source-aware": "Source-aware", "target-only": "Target-only",
    manual_review: "Manual review", bazarr_scan: "Bazarr scan",
    queue_retry: "Queue retry", recheck: "Recheck", dismiss: "Dismiss",
    dispatched: "Dispatched", pending: "Pending", claimed: "Dispatching",
    failed: "Failed", resolved: "Resolved", queued: "Queued", invalid: "Invalid",
  };

  const escapeRawHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const escapeHtml = (value) => escapeRawHtml(
    operatorLabels[String(value ?? "")] || (value ?? "\u2014"),
  );
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const label = (value) => String(value || "—").replaceAll("_", " ");
  const yesNo = (value) => value ? "Yes" : "No";
  const operatorLabel = (value) => operatorLabels[String(value || "")] || label(value);
  const time = (value) => {
    const parsed = new Date(number(value) * 1000);
    if (Number.isNaN(parsed.getTime())) return { text: "Unknown", iso: "" };
    const options = { timeZone: configuredTimeZone, dateStyle: "medium", timeStyle: "long" };
    let text;
    try { text = new Intl.DateTimeFormat(undefined, options).format(parsed); }
    catch (_error) { text = new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(parsed); }
    return { text, iso: parsed.toISOString() };
  };
  const timeMarkup = (value) => {
    const formatted = time(value);
    return formatted.iso
      ? `<time datetime="${escapeHtml(formatted.iso)}" title="${escapeHtml(formatted.iso)}">${escapeHtml(formatted.text)}</time>`
      : escapeHtml(formatted.text);
  };
  const technicalCode = (value) => value
    ? `<details class="technical-code"><summary>Technical code</summary><code>${escapeRawHtml(value)}</code></details>`
    : "";
  const labelledCode = (value) => value
    ? `${escapeHtml(operatorLabel(value))}${technicalCode(value)}`
    : "â€”";
  const availabilityText = (reason) => ({
    available: "Available inside a managed root",
    not_found: "File not found or no path is recorded",
    outside_managed_root: "Path is outside the configured managed roots",
    resolver_unavailable: "Availability could not be checked",
  }[reason] || "Availability is unknown");
  const statusLabel = (value) => ({
    needs_attention: "Needs attention",
    manually_queued: "Manually queued",
    resolved: "Resolved",
    dismissed: "Dismissed",
  }[value] || value || "Unknown");
  const statusTone = (value) => ({
    needs_attention: "badge-warning",
    manually_queued: "badge-accent",
    resolved: "badge-success",
    dismissed: "badge-default",
  }[value] || "badge-default");

  const currentTheme = () => document.documentElement.dataset.theme || "dark";
  const updateThemeButton = () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const next = currentTheme() === "dark" ? "light" : "dark";
    button.textContent = `${next === "light" ? "Light" : "Dark"} theme`;
    button.setAttribute("aria-label", `Switch to ${next} theme`);
  };
  const setTheme = (theme) => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("dashboard-theme", theme); } catch (_error) { /* optional */ }
    updateThemeButton();
  };
  const initialTheme = () => {
    let saved = null;
    try { saved = localStorage.getItem("dashboard-theme"); } catch (_error) { saved = null; }
    const system = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    setTheme(saved === "light" || saved === "dark" ? saved : system);
  };

  const captureFocus = () => {
    const active = document.activeElement;
    const key = active?.dataset?.focusKey;
    if (!key) return null;
    return {
      key,
      start: typeof active.selectionStart === "number" ? active.selectionStart : null,
      end: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
    };
  };

  const restoreFocus = () => {
    if (actionFocus?.type === "status") {
      document.getElementById("review-action-status")?.focus({ preventScroll: true });
      actionFocus = null;
      savedFocus = null;
      return;
    }
    const key = actionFocus?.key || savedFocus?.key;
    const target = key ? root.querySelector(`[data-focus-key="${CSS.escape(key)}"]`) : null;
    target?.focus({ preventScroll: true });
    const selection = actionFocus || savedFocus;
    if (target && selection?.start !== null && typeof target.setSelectionRange === "function") {
      target.setSelectionRange(selection.start, selection.end ?? selection.start);
    }
    actionFocus = null;
    savedFocus = null;
  };

  const filterValues = () => {
    const form = document.getElementById("review-filters");
    if (!form) return { page: "1", pageSize: "20", sort: "updatedAt", direction: "desc" };
    return Object.fromEntries(new FormData(form).entries());
  };
  const header = () => `<header class="topbar"><div><div class="eyebrow">Operator recovery</div>
    <h1>Manual review</h1><p class="header-meta">Restore files externally, verify them safely, or authorize one scheduler retry.</p></div>
    <div class="header-actions"><a class="btn btn-secondary" href="/" data-focus-key="nav-status">Status</a>
      <a class="btn btn-secondary" href="/review" aria-current="page" data-focus-key="nav-review">Manual review</a>
      <a class="btn btn-secondary" href="/logs" data-focus-key="nav-logs">Logs</a>
      <button class="btn btn-secondary" id="theme-toggle" type="button" data-focus-key="theme-toggle">Theme</button>
      <button class="btn btn-primary${loading ? " is-loading" : ""}" id="review-refresh" type="button"
        data-focus-key="review-refresh" ${loading ? 'disabled aria-busy="true"' : ""}>${loading ? "Refreshing…" : "Refresh now"}</button></div></header>`;

  const summaryCard = (text, value, tone = "") => (
    `<article class="review-summary-card ${tone}"><span>${escapeHtml(text)}</span>`
    + `<strong>${number(value).toLocaleString()}</strong></article>`
  );

  const completenessDetails = (completeness) => {
    if (!completeness || typeof completeness !== "object") return "";
    const rows = [
      ["Evaluated", yesNo(completeness.evaluated)],
      ["Undersized", yesNo(completeness.undersized)],
      ["Reason", completeness.reason ? operatorLabel(completeness.reason) : null],
      ["Media duration", completeness.mediaDurationSeconds == null ? null : `${completeness.mediaDurationSeconds}s`],
      ["Subtitle bytes", completeness.subtitleBytes],
      ["Cue count", completeness.cueCount],
      ["Dialogue characters", completeness.dialogueChars],
      ["Cues per minute", completeness.cuesPerMinute],
      ["Text characters per minute", completeness.textCharsPerMinute],
      ["Bytes per minute", completeness.bytesPerMinute],
      ["Timeline coverage", completeness.timelineCoverage],
      ["Failed signals", (completeness.failedSignals || []).map(operatorLabel).join(", ") || "None"],
      ["Thresholds", Object.entries(completeness.thresholds || {}).map(([key, value]) => `${label(key)}: ${value}`).join(", ") || "—"],
    ].filter(([, value]) => value !== null && value !== undefined);
    return `<dl class="review-audit-details">${rows.map(([name, value]) => (
      `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`
    )).join("")}</dl>`;
  };

  const auditDetails = (details) => {
    if (!details || typeof details !== "object" || !Object.keys(details).length) return "";
    const labels = {
      validationResult: "Validation result", validationMode: "Validation mode",
      issueRules: "Issue rules", observationCount: "Observations",
      sourceAvailable: "Source available", targetAvailable: "Target available",
      artifactAvailable: "Artifact available", mediaAvailable: "Media available",
      scanPending: "Scan pending",
    };
    const codeFields = new Set(["validationResult", "validationMode", "issueRules"]);
    const rows = Object.entries(details).filter(([key]) => key !== "completeness").map(([key, value]) => {
      const display = Array.isArray(value)
        ? value.map((entry) => codeFields.has(key) ? operatorLabel(entry) : entry).join(", ") || "None"
        : typeof value === "boolean"
          ? yesNo(value)
          : codeFields.has(key) ? operatorLabel(value) : value;
      return `<div><dt>${escapeHtml(labels[key] || label(key))}</dt><dd>${escapeHtml(display)}</dd></div>`;
    }).join("");
    const completeness = completenessDetails(details.completeness);
    return `${rows ? `<dl class="review-audit-details">${rows}</dl>` : ""}${completeness ? `<h5>Completeness</h5>${completeness}` : ""}`;
  };

  const actionHistory = (actions, count, truncated) => {
    if (!actions?.length) return '<p class="empty-state">No manual actions recorded.</p>';
    const notice = truncated
      ? `<p class="section-note">Showing the latest ${actions.length.toLocaleString()} of ${number(count).toLocaleString()} actions.</p>`
      : "";
    return `${notice}<ol class="review-history">${actions.map((entry) => (
      `<li><div><strong>${escapeHtml(label(entry.action))}</strong>`
      + ` <span class="badge badge-default">${escapeHtml(entry.outcome || "recorded")}</span></div>`
      + `<span>${timeMarkup(entry.createdAt)}</span>`
      + (entry.reasonCode ? `<div class="review-history-reason">${labelledCode(entry.reasonCode)}</div>` : "")
      + auditDetails(entry.details) + `</li>`
    )).join("")}</ol>`;
  };

  const details = (item) => {
    const paths = [
      ["Source", item.sourceRelativePath, item.sourceAvailable, item.sourceAvailabilityReason],
      ["Target", item.targetRelativePath, item.targetAvailable, item.targetAvailabilityReason],
      ["Recovery artifact", item.artifactRelativePath, item.artifactAvailable, item.artifactAvailabilityReason],
      ["Media", item.mediaRelativePath, item.mediaAvailable, item.mediaAvailabilityReason],
    ];
    const open = expandedReviewIds.has(String(item.id)) ? " open" : "";
    return `<details class="review-details" data-review-details="${item.id}"${open}><summary data-focus-key="review-${item.id}-details">Recovery details</summary>
      <dl class="review-detail-grid">
        <div><dt>Failure class</dt><dd>${escapeHtml(operatorLabel(item.failureClass))}</dd></div>
        <div><dt>Retries completed</dt><dd>${number(item.attemptCount).toLocaleString()}</dd></div>
        <div><dt>Validation rules</dt><dd>${escapeHtml((item.failureRules || []).map(operatorLabel).join(", ") || "—")}</dd></div>
        <div><dt>Recovered cues</dt><dd>${number(item.recovery?.validRecoveredCueCount).toLocaleString()}</dd></div>
        <div><dt>Unresolved cues</dt><dd>${number(item.recovery?.unresolvedCueCount).toLocaleString()}</dd></div>
        <div><dt>Recovery stage</dt><dd>${escapeHtml(operatorLabel(item.recovery?.latestRecoveryStage))}</dd></div>
        <div><dt>Validation outcome</dt><dd>${escapeHtml(operatorLabel(item.validationFeedback?.validationResult || item.validationFeedback?.outcome || "Not rechecked"))}</dd></div>
        <div><dt>Validation reason</dt><dd>${escapeHtml(operatorLabel(item.validationFeedback?.reasonCode))}</dd></div>
        <div><dt>Bazarr scan</dt><dd>${escapeHtml(item.scanPending ? "Pending delivery" : operatorLabel(item.scanState || "Not requested"))}</dd></div>
        <div class="review-detail-wide"><dt>Last reason</dt><dd>${escapeHtml(item.lastReason || "—")}</dd></div>
      </dl>
      <details class="technical-code review-technical-codes"><summary>Technical codes</summary>
        <dl class="review-audit-details">
          <div><dt>Failure class</dt><dd><code>${escapeRawHtml(item.failureClass || "")}</code></dd></div>
          <div><dt>Validation rules</dt><dd><code>${(item.failureRules || []).map(escapeRawHtml).join(", ")}</code></dd></div>
          <div><dt>Recovery stage</dt><dd><code>${escapeRawHtml(item.recovery?.latestRecoveryStage || "")}</code></dd></div>
          <div><dt>Validation reason</dt><dd><code>${escapeRawHtml(item.validationFeedback?.reasonCode || "")}</code></dd></div>
        </dl>
      </details>
      ${item.validationFeedback?.completeness ? `<h4>Completeness evidence</h4>${completenessDetails(item.validationFeedback.completeness)}` : ""}
      <h4>Managed paths</h4>
      <dl class="review-paths">${paths.map(([name, path, available, reason]) => (
        `<div><dt>${escapeHtml(name)}</dt><dd><span class="badge ${available ? "badge-success" : "badge-warning"}">`
        + `${available ? "Available" : "Unavailable"}</span><span class="review-availability-reason">${escapeHtml(availabilityText(reason))}</span>`
        + `${path ? `<code class="review-managed-path">${escapeHtml(path)}</code>` : ""}</dd></div>`
      )).join("")}</dl>
      <h4>Action history</h4>${actionHistory(item.actions, item.actionCount, item.actionsTruncated)}
    </details>`;
  };

  const actionButtons = (item) => {
    const actions = new Set(item.allowedActions || []);
    if (!actions.size) return '<span class="section-note">No actions available</span>';
    const disabled = !payload.actionsEnabled;
    const button = (action, text, className) => actions.has(action)
      ? `<button class="btn btn-sm ${className}" type="button" data-review-action="${action}" data-review-id="${item.id}"
          data-updated-at="${item.updatedAt}" data-focus-key="${item.id}-${action}"${disabled ? ' disabled title="Manual actions are disabled by configuration" aria-describedby="review-disabled-note"' : ""}>${text}</button>`
      : "";
    return `<div class="review-actions">
      ${button("recheck", "Recheck restored file", "btn-primary")}
      ${button("queue_retry", "Queue manual retry", "btn-secondary")}
      ${button("dismiss", "Dismiss", "btn-danger")}
    </div>`;
  };

  const rows = () => {
    if (!payload.items?.length) return '<p class="empty-state">No manual reviews match these filters.</p>';
    return `<div class="table-wrap review-table-wrap"><table class="data-table review-table">
      <thead><tr><th>Media</th><th>Type</th><th>Language</th><th>Status</th><th>Updated</th><th>Actions</th></tr></thead>
      ${payload.items.map((item) => {
        const title = item.media?.title || `${item.itemType || "media"} ${item.itemId}`;
        const episode = item.media?.episodeCode ? `<span>${escapeHtml(item.media.episodeCode)}</span>` : "";
        const expanded = expandedReviewIds.has(String(item.id));
        return `<tbody class="review-record"><tr class="review-main-row${expanded ? " has-expanded" : ""}">
          <td class="cell-media" data-label="Media"><strong>${escapeHtml(title)}</strong>${episode}</td>
          <td data-label="Type">${escapeHtml(operatorLabel(item.itemType || "media"))}</td>
          <td data-label="Language">${escapeHtml(item.targetLanguage || "—")}</td>
          <td data-label="Status"><span class="badge ${statusTone(item.status)}">${escapeHtml(statusLabel(item.status))}</span>${item.scanPending ? ' <span class="badge badge-warning">Scan pending</span>' : ""}</td>
          <td data-label="Updated">${timeMarkup(item.updatedAt)}</td>
          <td class="cell-actions" data-label="Actions">${actionButtons(item)}</td>
        </tr><tr class="review-detail-row"><td colspan="6">${details(item)}</td></tr></tbody>`;
      }).join("")}</table></div>`;
  };

  const filters = (values, page, pageSize) => `<form id="review-filters" class="review-filters">
    <label>Search<input name="q" maxlength="100" value="${escapeHtml(values.q || "")}" placeholder="Media, episode, language, or reason" data-focus-key="filter-q" ${loading ? "disabled" : ""}></label>
    <label>Status<select name="status" data-focus-key="filter-status" ${loading ? "disabled" : ""}><option value="">All statuses</option>${["needs_attention", "manually_queued", "resolved", "dismissed"].map((value) => `<option value="${value}" ${values.status === value ? "selected" : ""}>${statusLabel(value)}</option>`).join("")}</select></label>
    <label>Type<select name="itemType" data-focus-key="filter-type" ${loading ? "disabled" : ""}><option value="">All types</option><option value="episodes" ${values.itemType === "episodes" ? "selected" : ""}>Episodes</option><option value="movies" ${values.itemType === "movies" ? "selected" : ""}>Movies</option></select></label>
    <label>Language<input name="language" maxlength="20" value="${escapeHtml(values.language || "")}" placeholder="et" data-focus-key="filter-language" ${loading ? "disabled" : ""}></label>
    <label>Sort<select name="sort" data-focus-key="filter-sort" ${loading ? "disabled" : ""}>${[["updatedAt", "Updated"], ["media", "Media"], ["language", "Language"], ["attempts", "Retries completed"], ["status", "Status"]].map(([value, text]) => `<option value="${value}" ${values.sort === value ? "selected" : ""}>${text}</option>`).join("")}</select></label>
    <label>Direction<select name="direction" data-focus-key="filter-direction" ${loading ? "disabled" : ""}><option value="desc" ${values.direction !== "asc" ? "selected" : ""}>Descending</option><option value="asc" ${values.direction === "asc" ? "selected" : ""}>Ascending</option></select></label>
    <input type="hidden" name="page" value="${page}"><input type="hidden" name="pageSize" value="${pageSize}">
    <div class="review-filter-actions"><button class="btn btn-primary" type="submit" data-focus-key="filter-apply" ${loading ? "disabled" : ""}>Apply filters</button>
      <button class="btn btn-secondary" id="review-clear" type="button" data-focus-key="filter-clear" ${loading ? "disabled" : ""}>Clear filters</button></div>
  </form>`;

  const renderInitialError = () => {
    root.innerHTML = `${header()}<section class="panel review-unavailable" role="alert"><h2>Manual reviews are unavailable</h2>
      <p>${escapeHtml(loadError || "The review service could not be reached.")}</p>
      <button class="btn btn-primary" id="review-retry" type="button" data-focus-key="review-retry">Retry</button></section>`;
    root.setAttribute("aria-busy", "false");
    bind();
    updateThemeButton();
    restoreFocus();
  };

  const render = () => {
    if (!payload) {
      if (viewState === "initial-error") renderInitialError();
      return;
    }
    const values = filterState;
    const counts = payload.counts || {};
    const pagination = payload.pagination || {};
    const page = number(pagination.page) || 1;
    const pageSize = number(pagination.pageSize) || 20;
    const total = number(pagination.total);
    root.innerHTML = `${header()}
      ${viewState === "refresh-error" ? `<p class="review-error" role="alert">Could not refresh manual reviews. Existing data may be stale. ${escapeHtml(loadError)}</p>` : ""}
      ${payload.actionsEnabled ? "" : '<p class="review-notice" id="review-disabled-note" role="status">Manual actions are disabled. Review records and controls remain available in read-only form.</p>'}
      <section class="review-summary" aria-label="Manual review summary">
        ${summaryCard("Needs attention", counts.needsAttention, "tone-warning")}
        ${summaryCard("Manually queued", counts.manuallyQueued, "tone-accent")}
        ${summaryCard("Resolved", counts.resolved, "tone-success")}
        ${summaryCard("Dismissed", counts.dismissed)}
      </section>
      <section class="panel">${filters(values, page, pageSize)}
      <p id="review-action-status" class="review-action-status ${actionError ? "is-error" : ""}" role="${actionError ? "alert" : "status"}" aria-live="${actionError ? "assertive" : "polite"}" tabindex="-1">${escapeHtml(actionMessage || `${total.toLocaleString()} review record${total === 1 ? "" : "s"}`)}</p>
      ${rows()}
      <nav class="review-pagination" aria-label="Manual review pages">
        <button class="btn btn-secondary" id="review-prev" type="button" data-focus-key="review-prev" ${page <= 1 || loading ? "disabled" : ""}>Previous</button>
        <span>Page ${page.toLocaleString()} &middot; ${total.toLocaleString()} records</span>
        <button class="btn btn-secondary" id="review-next" type="button" data-focus-key="review-next" ${(page * pageSize) >= total || loading ? "disabled" : ""}>Next</button>
      </nav></section>
      <p class="footer-note">Trusted LAN endpoint &middot; no subtitle text, hashes, credentials, or absolute paths exposed</p>`;
    root.setAttribute("aria-busy", loading ? "true" : "false");
    bind();
    updateThemeButton();
    restoreFocus();
  };

  const setLoadingUi = () => {
    root.setAttribute("aria-busy", "true");
    root.querySelectorAll(
      "#review-refresh, #review-retry, #review-filters button, #review-filters input, "
      + "#review-filters select, #review-prev, #review-next, [data-review-action]",
    ).forEach((control) => { control.disabled = true; });
    const refresh = document.getElementById("review-refresh");
    if (refresh) {
      refresh.classList.add("is-loading");
      refresh.setAttribute("aria-busy", "true");
      refresh.textContent = "Refreshing…";
    }
  };

  const load = async (requestedQuery = null) => {
    if (loading) return;
    if (requestedQuery === null) filterState = filterValues();
    const query = requestedQuery ?? new URLSearchParams(filterState).toString();
    savedFocus = actionFocus ? null : captureFocus();
    loading = true;
    setLoadingUi();
    try {
      const response = await fetch(`/api/manual-reviews?${query}`, { cache: "no-store", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || `Manual reviews unavailable (${response.status})`);
      payload = data;
      filterState.page = String(number(data.pagination?.page) || 1);
      filterState.pageSize = String(number(data.pagination?.pageSize) || 20);
      viewState = "ready";
      loadError = "";
    } catch (error) {
      loadError = error instanceof Error ? error.message : "Manual reviews are unavailable.";
      viewState = payload ? "refresh-error" : "initial-error";
    } finally {
      loading = false;
      render();
    }
  };

  const act = async (button) => {
    const action = button.dataset.reviewAction;
    const labels = { queue_retry: "queue one manual retry", dismiss: "dismiss this review" };
    if (labels[action] && !confirm(`Are you sure you want to ${labels[action]}?`)) return;
    filterState = filterValues();
    const currentQuery = new URLSearchParams(filterState).toString();
    const origin = { key: button.dataset.focusKey || "", start: null, end: null };
    actionFocus = origin;
    loading = true;
    setLoadingUi();
    button.textContent = action === "recheck" ? "Rechecking…" : action === "dismiss" ? "Dismissing…" : "Queueing…";
    try {
      const response = await fetch(`/api/manual-reviews/${button.dataset.reviewId}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-Bazarr-Autotranslate-Action": "manual-review" },
        body: JSON.stringify({ action, expectedUpdatedAt: number(button.dataset.updatedAt) }),
      });
      const data = await response.json();
      if (!response.ok && response.status !== 202) throw new Error(data.error?.message || `Action failed (${response.status})`);
      actionMessage = data.scanPending ? "File accepted; Bazarr scan is queued for retry." : ({ queued: "Manual retry queued for scheduler admission.", dismissed: "Review dismissed.", invalid: "The restored file is still invalid.", resolved: "Restored file accepted and Bazarr scan dispatched." }[data.outcome] || "Action completed.");
      actionError = data.outcome === "invalid";
      actionFocus = data.outcome === "invalid" ? origin : { type: "status" };
    } catch (error) {
      actionMessage = error instanceof Error ? error.message : "Action failed.";
      actionError = true;
      actionFocus = origin;
    } finally {
      loading = false;
    }
    await load(currentQuery);
  };

  const changePage = (delta) => {
    const input = document.querySelector('#review-filters input[name="page"]');
    input.value = String(Math.max(1, number(input.value) + delta));
    load();
  };

  const bind = () => {
    document.getElementById("theme-toggle")?.addEventListener("click", () => setTheme(currentTheme() === "dark" ? "light" : "dark"));
    document.getElementById("review-refresh")?.addEventListener("click", () => load());
    document.getElementById("review-retry")?.addEventListener("click", () => load());
    document.getElementById("review-filters")?.addEventListener("submit", (event) => {
      event.preventDefault();
      event.currentTarget.elements.page.value = "1";
      load();
    });
    document.getElementById("review-clear")?.addEventListener("click", () => {
      filterState = { page: "1", pageSize: "20", sort: "updatedAt", direction: "desc" };
      actionFocus = { key: "filter-q", start: 0, end: 0 };
      load(new URLSearchParams(filterState).toString());
    });
    document.getElementById("review-prev")?.addEventListener("click", () => changePage(-1));
    document.getElementById("review-next")?.addEventListener("click", () => changePage(1));
    document.querySelectorAll("[data-review-action]").forEach((button) => button.addEventListener("click", () => act(button)));
    document.querySelectorAll("[data-review-details]").forEach((disclosure) => disclosure.addEventListener("toggle", () => {
      const id = disclosure.dataset.reviewDetails;
      if (disclosure.open) expandedReviewIds.add(id); else expandedReviewIds.delete(id);
      disclosure.closest(".review-record")?.querySelector(".review-main-row")?.classList.toggle("has-expanded", disclosure.open);
    }));
  };

  initialTheme();
  load();
  setInterval(() => { if (!document.hidden && !loading) load(); }, 20_000);
})();
