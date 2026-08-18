import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { requestJson } from "../shared/api";
import { AdvancedFilters, AppHeader, Panel, PanelBody } from "../shared/components";

type LogPayload = { lines?: string[]; nextCursor?: number | null; sanitized?: boolean };
type Filters = { level: string; job: string; q: string };
const EMPTY_FILTERS: Filters = { level: "", job: "", q: "" };

export function LogsApp() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [lines, setLines] = useState<string[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("Loading logs…");
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async (append = false, values = filters) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setMessage("Loading logs…");
    const query = new URLSearchParams(values);
    query.set("limit", "200");
    if (append && cursor !== null) query.set("cursor", String(cursor));
    try {
      const payload = await requestJson<LogPayload>(`/api/logs?${query}`, {}, controller.signal);
      if (controller.signal.aborted) return;
      const next = payload.lines || [];
      setLines((current) => append ? [...current, ...next] : next);
      setCursor(payload.nextCursor ?? null);
      const count = append ? lines.length + next.length : next.length;
      setMessage(count ? `Showing ${count.toLocaleString()} sanitized records` : "No matching log records.");
    } catch (error) {
      if (controller.signal.aborted) return;
      setMessage(error instanceof Error ? error.message : "Logs unavailable");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [cursor, filters, lines.length]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCursor(null);
    void load(false);
  };

  useEffect(() => {
    void load(false, EMPTY_FILTERS);
    return () => abortRef.current?.abort();
    // Initial data is intentionally loaded once; later requests are user initiated.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <main className="dashboard-shell log-shell">
    <AppHeader
      eyebrow="Diagnostics"
      title="Service logs"
      description="Sanitized, read-only operational output · New records use UTC timestamps"
      current="logs"
      action={<button className="btn btn-primary" type="button" disabled={loading} onClick={() => void load(false)}>
        {loading ? "Refreshing…" : "Refresh now"}
      </button>}
    />
    <Panel>
      <PanelBody className="log-panel-body"><form className="log-filters" onSubmit={submit}>
        <label className="filter-search">Search text <input value={filters.q} maxLength={100} placeholder="Message contains…" onChange={(event) => setFilters({ ...filters, q: event.target.value })} /></label>
        <AdvancedFilters><label>Level <select value={filters.level} onChange={(event) => setFilters({ ...filters, level: event.target.value })}>
            <option value="">All</option><option>ERROR</option><option>WARNING</option><option>FAIL</option><option>TIMEOUT</option>
          </select></label>
          <label>Show or job <input value={filters.job} maxLength={100} placeholder="Top Gear or job ID" onChange={(event) => setFilters({ ...filters, job: event.target.value })} /></label></AdvancedFilters>
        <button className="btn btn-primary" type="submit" disabled={loading}>Filter</button>
      </form>
      <p className="section-note" role="status">{message}</p>
      <pre className="log-output" tabIndex={0} aria-busy={loading}>{lines.join("\n")}</pre>
      {cursor !== null && <button className="btn btn-secondary" type="button" disabled={loading} onClick={() => void load(true)}>Load older</button>}</PanelBody>
    </Panel>
  </main>;
}
