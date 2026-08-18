import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { requestJson } from "../shared/api";
import { AppHeader, PageFooter, Panel } from "../shared/components";
import { numberValue, operatorLabel, ReviewTime, statusLabel, statusTone } from "./format";
import { ReviewDetails } from "./ReviewDetails";
import { DEFAULT_FILTERS, type ActionPayload, type ReviewActionName, type ReviewFilters, type ReviewItem, type ReviewPayload } from "./types";

function queryString(filters: ReviewFilters) {
  return new URLSearchParams(filters).toString();
}

function actionResult(payload: ActionPayload) {
  if (payload.scanPending) return "File accepted; Bazarr scan is queued for retry.";
  return ({
    queued: "Manual retry queued for scheduler admission.",
    dismissed: "Review dismissed.",
    invalid: "The restored file is still invalid.",
    resolved: "Restored file accepted and Bazarr scan dispatched.",
  } as Record<string, string>)[payload.outcome || ""] || "Action completed.";
}

export function ReviewApp({ timeZone = "UTC", pollInterval = 20_000 }: { timeZone?: string; pollInterval?: number }) {
  const [payload, setPayload] = useState<ReviewPayload | null>(null);
  const [filters, setFilters] = useState<ReviewFilters>(DEFAULT_FILTERS);
  const [appliedFilters, setAppliedFilters] = useState<ReviewFilters>(DEFAULT_FILTERS);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [actionError, setActionError] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const abortRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const appliedRef = useRef(appliedFilters);
  const statusRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => { appliedRef.current = appliedFilters; }, [appliedFilters]);

  const load = useCallback(async (requested: ReviewFilters) => {
    if (busyRef.current) return false;
    busyRef.current = true;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    try {
      const data = await requestJson<ReviewPayload>(`/api/manual-reviews?${queryString(requested)}`, {}, controller.signal);
      if (controller.signal.aborted) return false;
      const next = {
        ...requested,
        page: String(numberValue(data.pagination?.page) || 1),
        pageSize: String(numberValue(data.pagination?.pageSize) || 20),
      };
      setPayload(data);
      setAppliedFilters(next);
      setFilters(next);
      setLoadError("");
      return true;
    } catch (error) {
      if (controller.signal.aborted) return false;
      setLoadError(error instanceof Error ? error.message : "Manual reviews are unavailable.");
      return false;
    } finally {
      if (abortRef.current === controller) {
        busyRef.current = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load(DEFAULT_FILTERS);
    const timer = window.setInterval(() => {
      if (!document.hidden && !busyRef.current) void load(appliedRef.current);
    }, pollInterval);
    return () => {
      window.clearInterval(timer);
      const activeRequest = abortRef.current;
      activeRequest?.abort();
      if (abortRef.current === activeRequest) busyRef.current = false;
    };
  }, [load, pollInterval]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = { ...filters, page: "1" };
    setFilters(next);
    void load(next);
  };

  const changePage = (delta: number) => {
    const next = { ...appliedFilters, page: String(Math.max(1, numberValue(appliedFilters.page) + delta)) };
    setFilters(next);
    void load(next);
  };

  const performAction = async (item: ReviewItem, action: ReviewActionName, button: HTMLButtonElement) => {
    const confirmations: Partial<Record<ReviewActionName, string>> = {
      queue_retry: "queue one manual retry",
      dismiss: "dismiss this review",
    };
    if (confirmations[action] && !window.confirm(`Are you sure you want to ${confirmations[action]}?`)) return;
    if (busyRef.current) return;
    busyRef.current = true;
    setLoading(true);
    setActionMessage("");
    setActionError(false);
    try {
      const result = await requestJson<ActionPayload>(`/api/manual-reviews/${item.id}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review" },
        body: JSON.stringify({ action, expectedUpdatedAt: numberValue(item.updatedAt) }),
      });
      setActionMessage(actionResult(result));
      setActionError(result.outcome === "invalid");
      busyRef.current = false;
      setLoading(false);
      await load(appliedRef.current);
      window.setTimeout(() => {
        if (document.body.contains(button)) button.focus({ preventScroll: true });
        else statusRef.current?.focus({ preventScroll: true });
      });
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : "Action failed.");
      setActionError(true);
      busyRef.current = false;
      setLoading(false);
      window.setTimeout(() => button.focus({ preventScroll: true }));
    }
  };

  const updateFilter = (key: keyof ReviewFilters, value: string) => setFilters((current) => ({ ...current, [key]: value }));

  if (!payload && loading) return <main className="dashboard-shell review-shell" aria-busy="true"><h1>Manual review</h1><p className="loading">Loading manual reviews…</p></main>;

  if (!payload) return <main className="dashboard-shell review-shell">
    <AppHeader eyebrow="Operator recovery" title="Manual review" description="Restore files externally, verify them safely, or authorize one scheduler retry." current="review" />
    <Panel className="review-unavailable"><div role="alert"><h2>Manual reviews are unavailable</h2><p>{loadError || "The review service could not be reached."}</p></div>
      <button className="btn btn-primary" type="button" onClick={() => void load(appliedFilters)}>Retry</button>
    </Panel>
  </main>;

  const counts = payload.counts || {};
  const page = numberValue(payload.pagination?.page) || 1;
  const pageSize = numberValue(payload.pagination?.pageSize) || 20;
  const total = numberValue(payload.pagination?.total);

  return <main className="dashboard-shell review-shell" aria-busy={loading}>
    <AppHeader
      eyebrow="Operator recovery"
      title="Manual review"
      description="Restore files externally, verify them safely, or authorize one scheduler retry."
      current="review"
      action={<button className={`btn btn-primary${loading ? " is-loading" : ""}`} type="button" disabled={loading} aria-busy={loading} onClick={() => void load(appliedFilters)}>{loading ? "Refreshing…" : "Refresh now"}</button>}
    />
    {loadError && <p className="review-error" role="alert">Could not refresh manual reviews. Existing data may be stale. {loadError}</p>}
    {!payload.actionsEnabled && <p className="review-notice" id="review-disabled-note" role="status">Manual actions are disabled. Review records and controls remain available in read-only form.</p>}
    <section className="review-summary" aria-label="Manual review summary">
      {[ ["Needs attention", counts.needsAttention, "tone-warning"], ["Manually queued", counts.manuallyQueued, "tone-accent"], ["Resolved", counts.resolved, "tone-success"], ["Dismissed", counts.dismissed, ""] ].map(([label, value, tone]) =>
        <article className={`review-summary-card ${tone}`} key={String(label)}><span>{label}</span><strong>{numberValue(value).toLocaleString()}</strong></article>)}
    </section>
    <Panel>
      <form className="review-filters" onSubmit={submit}>
        <label>Search<input maxLength={100} value={filters.q} placeholder="Media, episode, language, or reason" disabled={loading} onChange={(event) => updateFilter("q", event.target.value)} /></label>
        <label>Status<select value={filters.status} disabled={loading} onChange={(event) => updateFilter("status", event.target.value)}><option value="">All statuses</option>{["needs_attention", "manually_queued", "resolved", "dismissed"].map((value) => <option value={value} key={value}>{statusLabel(value)}</option>)}</select></label>
        <label>Type<select value={filters.itemType} disabled={loading} onChange={(event) => updateFilter("itemType", event.target.value)}><option value="">All types</option><option value="episodes">Episodes</option><option value="movies">Movies</option></select></label>
        <label>Language<input maxLength={20} value={filters.language} placeholder="et" disabled={loading} onChange={(event) => updateFilter("language", event.target.value)} /></label>
        <label>Sort<select value={filters.sort} disabled={loading} onChange={(event) => updateFilter("sort", event.target.value)}>{[["updatedAt", "Updated"], ["media", "Media"], ["language", "Language"], ["attempts", "Retries completed"], ["status", "Status"]].map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></label>
        <label>Direction<select value={filters.direction} disabled={loading} onChange={(event) => updateFilter("direction", event.target.value)}><option value="desc">Descending</option><option value="asc">Ascending</option></select></label>
        <div className="review-filter-actions"><button className="btn btn-primary" type="submit" disabled={loading}>Apply filters</button>
          <button className="btn btn-secondary" type="button" disabled={loading} onClick={() => { setFilters(DEFAULT_FILTERS); void load(DEFAULT_FILTERS); }}>Clear filters</button></div>
      </form>
      <p ref={statusRef} className={`review-action-status ${actionError ? "is-error" : ""}`} role={actionError ? "alert" : "status"} aria-live={actionError ? "assertive" : "polite"} tabIndex={-1}>
        {actionMessage || `${total.toLocaleString()} review record${total === 1 ? "" : "s"}`}
      </p>
      {!payload.items?.length ? <p className="empty-state">No manual reviews match these filters.</p> : <div className="table-wrap review-table-wrap"><table className="data-table review-table">
        <thead><tr><th>Media</th><th>Type</th><th>Language</th><th>Status</th><th>Updated</th><th>Actions</th></tr></thead>
        {payload.items.map((item) => {
          const open = expandedIds.has(item.id);
          const title = item.media?.title || `${item.itemType || "media"} ${item.itemId}`;
          return <tbody className="review-record" key={item.id}>
            <tr className={`review-main-row${open ? " has-expanded" : ""}`}>
              <td className="cell-media" data-label="Media"><strong>{title}</strong>{item.media?.episodeCode && <span>{item.media.episodeCode}</span>}</td>
              <td data-label="Type">{operatorLabel(item.itemType || "media")}</td>
              <td data-label="Language">{item.targetLanguage || "—"}</td>
              <td data-label="Status"><span className={`badge ${statusTone(item.status)}`}>{statusLabel(item.status)}</span>{item.scanPending && <> <span className="badge badge-warning">Scan pending</span></>}</td>
              <td data-label="Updated"><ReviewTime value={item.updatedAt} timeZone={timeZone} /></td>
              <td className="cell-actions" data-label="Actions"><div className="review-actions">
                {!item.allowedActions?.length && <span className="section-note">No actions available</span>}
                {item.allowedActions?.includes("recheck") && <button className="btn btn-sm btn-primary" type="button" disabled={loading || !payload.actionsEnabled} aria-describedby={!payload.actionsEnabled ? "review-disabled-note" : undefined} onClick={(event) => void performAction(item, "recheck", event.currentTarget)}>Recheck restored file</button>}
                {item.allowedActions?.includes("queue_retry") && <button className="btn btn-sm btn-secondary" type="button" disabled={loading || !payload.actionsEnabled} aria-describedby={!payload.actionsEnabled ? "review-disabled-note" : undefined} onClick={(event) => void performAction(item, "queue_retry", event.currentTarget)}>Queue manual retry</button>}
                {item.allowedActions?.includes("dismiss") && <button className="btn btn-sm btn-danger" type="button" disabled={loading || !payload.actionsEnabled} aria-describedby={!payload.actionsEnabled ? "review-disabled-note" : undefined} onClick={(event) => void performAction(item, "dismiss", event.currentTarget)}>Dismiss</button>}
              </div></td>
            </tr>
            <tr className="review-detail-row"><td colSpan={6}><ReviewDetails item={item} timeZone={timeZone} open={open} onToggle={(nextOpen) => setExpandedIds((current) => { const next = new Set(current); if (nextOpen) next.add(item.id); else next.delete(item.id); return next; })} /></td></tr>
          </tbody>;
        })}
      </table></div>}
      <nav className="review-pagination" aria-label="Manual review pages">
        <button className="btn btn-secondary" type="button" disabled={page <= 1 || loading} onClick={() => changePage(-1)}>Previous</button>
        <span>Page {page.toLocaleString()} · {total.toLocaleString()} records</span>
        <button className="btn btn-secondary" type="button" disabled={(page * pageSize) >= total || loading} onClick={() => changePage(1)}>Next</button>
      </nav>
    </Panel>
    <PageFooter />
  </main>;
}
