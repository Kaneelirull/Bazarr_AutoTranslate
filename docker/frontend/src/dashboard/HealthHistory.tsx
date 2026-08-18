import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Observations } from "./Observations";
import { Diagnostics, LatestMaintenance, RecoveryDiagnostics } from "./Sections";
import { numberValue } from "./format";
import type { DataRow } from "./types";

type HealthTab = "health" | "observations" | "history";
type HistoryKind = "outcomes" | "maintenance";
type HistoryWindow = "1h" | "6h" | "12h" | "24h" | "7d";

const HISTORY_WINDOWS: HistoryWindow[] = ["1h", "6h", "12h", "24h", "7d"];
const OUTCOME_LABELS: Array<[string, string, string]> = [
  ["Accepted", "accepted", "tone-success"], ["Repaired", "repaired", "tone-success"],
  ["Failed", "failed", "tone-danger"], ["Timed out", "timed_out", "tone-danger"],
  ["Waiting for retry", "waiting_retry", "tone-warning"], ["Circuit protected", "series_protected", "tone-warning"],
  ["Missing source", "missing_source", "tone-warning"], ["Deferred", "deferred", "tone-warning"],
  ["Quarantined", "quarantined", "tone-danger"],
];
const MAINTENANCE_LABELS: Array<[string, string, string]> = [
  ["Formatted", "formatted", ""], ["Repaired", "repaired", "tone-success"], ["Quarantined", "quarantined", "tone-danger"],
  ["Deleted", "deleted", ""], ["Undersized", "undersized", "tone-warning"], ["Pruned", "pruned", ""],
  ["Source-less warnings", "source_less_warnings", "tone-warning"], ["Repeat quarantines", "repeat_quarantines", "tone-warning"],
  ["Same-cycle suppressions", "cycle_suppressions", ""], ["Variant outputs", "variant_outputs", ""], ["Failures", "failures", "tone-danger"],
];

export function attentionReasons(cycle: DataRow, circuits: DataRow[], diagnostics: DataRow, observations: DataRow[], maintenance: DataRow) {
  const reasons: string[] = [];
  if (numberValue(cycle.failed) > 0) reasons.push("failed work");
  if (numberValue(cycle.timedOut) > 0) reasons.push("timed out work");
  if (numberValue(cycle.quarantined) > 0) reasons.push("quarantined work");
  if (circuits.some((entry) => ["open", "half_open", "eligible"].includes(entry.state))) reasons.push("active protection");
  if (numberValue(diagnostics.providerHealth?.malformed_response) > 0) reasons.push("provider responses");
  if (numberValue(maintenance.lastScan?.metrics?.failures) > 0 || (maintenance.recentOutcomes || []).some((entry: DataRow) => entry.outcome === "failed")) reasons.push("maintenance failures");
  if (observations.length > 0) reasons.push("validation observations");
  return reasons;
}

function HistoryView({ outcomes = {}, maintenance = {} }: { outcomes?: Record<string, DataRow>; maintenance?: Record<string, DataRow> }) {
  const [kind, setKind] = useState<HistoryKind>("outcomes");
  const [window, setWindow] = useState<HistoryWindow>("24h");
  const values = (kind === "outcomes" ? outcomes : maintenance)[window] || {};
  const labels = kind === "outcomes" ? OUTCOME_LABELS : MAINTENANCE_LABELS;
  return <section className="history-view" aria-labelledby="history-view-title">
    <div className="history-toolbar"><div><h3 id="history-view-title">Rolling history</h3><p className="section-note">One focused window at a time.</p></div>
      <div className="history-controls"><div className="segmented-control" role="group" aria-label="History type">{(["outcomes", "maintenance"] as HistoryKind[]).map((value) => <button type="button" aria-pressed={kind === value} onClick={() => setKind(value)} key={value}>{value === "outcomes" ? "Outcomes" : "Maintenance"}</button>)}</div>
        <label>Window<select aria-label="History window" value={window} onChange={(event) => setWindow(event.target.value as HistoryWindow)}>{HISTORY_WINDOWS.map((value) => <option value={value} key={value}>{value}</option>)}</select></label></div>
    </div>
    <div className="history-summary" aria-live="polite">{labels.map(([label, key, tone]) => { const count = numberValue(values[key]); return <div className={`outcome-line ${tone} ${count ? "" : "is-zero"}`} key={key}><span>{label}</span><strong>{count.toLocaleString()}</strong></div>; })}</div>
  </section>;
}

export function HealthHistory({ cycle, timing, circuits, diagnostics, observations, history, maintenance, timeZone }: { cycle: DataRow; timing?: DataRow; circuits?: DataRow[]; diagnostics?: DataRow; observations?: DataRow[]; history?: Record<string, DataRow>; maintenance: DataRow; timeZone: string }) {
  const safeCircuits = circuits || [];
  const safeDiagnostics = diagnostics || {};
  const safeObservations = observations || [];
  const reasons = attentionReasons(cycle, safeCircuits, safeDiagnostics, safeObservations, maintenance);
  const attention = reasons.length > 0;
  const [open, setOpen] = useState(attention);
  const [tab, setTab] = useState<HealthTab>(attention && safeObservations.length ? "observations" : "health");
  const previousAttention = useRef(attention);

  useEffect(() => {
    if (attention && !previousAttention.current) setOpen(true);
    if (!attention && previousAttention.current) setOpen(false);
    previousAttention.current = attention;
  }, [attention]);

  const tabs: Array<[HealthTab, string]> = [["health", "Health"], ["observations", `Observations ${safeObservations.length}`], ["history", "History"]];
  const moveTab = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    setTab(tabs[next][0]);
    document.getElementById(`health-tab-${tabs[next][0]}`)?.focus();
  };
  return <details className={`panel health-history ${attention ? "has-attention" : "is-healthy"}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><span><strong>Health & history</strong><small>{attention ? `${reasons.length} area${reasons.length === 1 ? "" : "s"} ${reasons.length === 1 ? "needs" : "need"} attention` : "All systems healthy"}</small></span><span className={`health-state ${attention ? "tone-warning" : "tone-success"}`}>{attention ? "Review" : "Healthy"}</span></summary>
    <div className="health-history-body"><div className="section-tabs" role="tablist" aria-label="Health and history views">{tabs.map(([value, label], index) => <button type="button" role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} aria-controls={`health-panel-${value}`} id={`health-tab-${value}`} onClick={() => setTab(value)} onKeyDown={(event) => moveTab(event, index)} key={value}>{label}</button>)}</div>
      <div role="tabpanel" id="health-panel-health" aria-labelledby="health-tab-health" hidden={tab !== "health"}><div className="health-stack"><Diagnostics timing={timing} circuits={safeCircuits} /><RecoveryDiagnostics diagnostics={safeDiagnostics} /><LatestMaintenance maintenance={maintenance} /></div></div>
      <div role="tabpanel" id="health-panel-observations" aria-labelledby="health-tab-observations" hidden={tab !== "observations"}><Observations observations={safeObservations} timeZone={timeZone} /></div>
      <div role="tabpanel" id="health-panel-history" aria-labelledby="health-tab-history" hidden={tab !== "history"}><HistoryView outcomes={history} maintenance={maintenance.history} /></div>
    </div>
  </details>;
}
