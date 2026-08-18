import { useState, type ReactNode } from "react";
import { formatDuration, relativeTime } from "../shared/time";
import { DataTable } from "./DataTable";
import { labelForState, Media, numberValue, operationLabel, TimeValue } from "./format";
import type { DataRow } from "./types";

export function PanelHeader({ title, note = "", actions }: { title: string; note?: ReactNode; actions?: ReactNode }) {
  return <div className="panel-header"><div><h2>{title}</h2>{note && <p className="section-note">{note}</p>}</div>{actions}</div>;
}

function Metric({ label, value, tone = "" }: { label: string; value: unknown; tone?: string }) {
  return <div className={`metric ${tone}`}><span className="metric-label">{label}</span><strong className="metric-value">{String(value)}</strong></div>;
}

export function Overview({ cycle, service }: { cycle: DataRow; service: DataRow }) {
  const initial = numberValue(cycle.initial);
  const done = numberValue(cycle.done);
  const percent = initial ? Math.round((done / initial) * 100) : 0;
  return <section className="panel overview" aria-labelledby="cycle-overview-title"><div className="overview-grid">
    <div><div className="progress-kicker" id="cycle-overview-title">Current cycle</div>
      <div className="progress-copy">{done.toLocaleString()} of {initial.toLocaleString()} complete <span>· {percent}%</span></div>
      <progress max={Math.max(initial, 1)} value={Math.min(done, Math.max(initial, 1))} aria-label="Cycle completion">{percent}%</progress>
      <div className="overview-facts">
        <div className="fact"><span className="fact-label">Remaining</span><strong className="fact-value">{numberValue(cycle.remaining).toLocaleString()}</strong></div>
        <div className="fact"><span className="fact-label">Elapsed</span><strong className="fact-value">{formatDuration(cycle.elapsedSeconds)}</strong></div>
        <div className="fact"><span className="fact-label">Approx. ETA</span><strong className="fact-value">{formatDuration(cycle.etaSeconds)}</strong></div>
        <div className="fact"><span className="fact-label">Next cycle</span><strong className="fact-value">{service.nextCycleAt ? relativeTime(service.nextCycleAt) : "—"}</strong></div>
      </div>
    </div>
    <div><div className="metric-group"><h3>Pipeline</h3><div className="metric-grid">
      <Metric label="Queued" value={numberValue(cycle.queued).toLocaleString()} />
      <Metric label="Translating" value={numberValue(cycle.translating).toLocaleString()} tone="tone-accent" />
      <Metric label="Validating" value={numberValue(cycle.validating).toLocaleString()} tone="tone-accent" />
      <Metric label="Repairing" value={numberValue(cycle.repairing).toLocaleString()} tone="tone-warning" />
    </div></div>
    <div className="metric-group"><h3>Outcomes</h3><div className="metric-grid outcomes">
      <Metric label="Accepted" value={numberValue(cycle.accepted).toLocaleString()} tone="tone-success" />
      <Metric label="Failed" value={numberValue(cycle.failed).toLocaleString()} tone="tone-danger" />
      <Metric label="Timed out" value={numberValue(cycle.timedOut).toLocaleString()} tone="tone-danger" />
      <Metric label="Waiting for retry" value={numberValue(cycle.waitingRetry).toLocaleString()} tone="tone-warning" />
      <Metric label="Circuit protected" value={numberValue(cycle.seriesProtected).toLocaleString()} tone="tone-warning" />
      <Metric label="Missing source" value={numberValue(cycle.missingSource).toLocaleString()} tone="tone-warning" />
      <Metric label="Deferred" value={numberValue(cycle.deferred).toLocaleString()} tone="tone-warning" />
      <Metric label="Quarantined" value={numberValue(cycle.quarantined).toLocaleString()} tone="tone-danger" />
    </div></div></div>
  </div></section>;
}

export function Diagnostics({ timing = {}, circuits = [] }: { timing?: DataRow; circuits?: DataRow[] }) {
  const timingBlock = (title: string, entry: DataRow = {}) => {
    const samples = numberValue(entry.sampleCount);
    const rate = Number.isFinite(Number(entry.secondsPerCue)) ? `~${Number(entry.secondsPerCue).toFixed(1)} sec/cue` : "—";
    return <article className="timing-block" key={title}><div className="timing-block-copy"><span className="timing-kind">{title}</span><span className="timing-basis">{samples ? "Learned average" : "Cold-start estimate"}</span></div>
      <div className="timing-reading"><strong>{rate}</strong><span>{samples.toLocaleString()} {samples === 1 ? "sample" : "samples"}</span></div></article>;
  };
  const active = circuits.filter((entry) => ["open", "half_open", "eligible"].includes(entry.state) && entry.seriesTitle);
  return <section className="panel"><PanelHeader title="Timing & protection" note="Adaptive estimates and series circuit-breaker status." />
    <div className="diagnostics-content"><div className="timing-grid">{timingBlock("File translation", timing.file)}{timingBlock("Cue repair", timing.repair)}</div>
      {active.length ? <div className="protection-row is-warning" role="status"><span className="protection-badge">Protection active</span><div className="protection-copy"><strong>Some series are temporarily paused</strong><span>Other translations and cue repairs continue normally.</span></div>
        <div className="protection-series-list">{active.map((entry) => { const failures = numberValue(entry.failures); const remaining = Math.max(0, numberValue(entry.completedCyclesRemaining)); const trial = entry.state === "half_open" && entry.trialState === "validation_pending" ? "Trial awaiting repair/validation" : entry.state === "half_open" && entry.trialJobId != null ? "Trial in progress" : remaining ? `Trial in ${remaining} ${remaining === 1 ? "cycle" : "cycles"}` : "Trial ready"; return <div className="protection-series" key={entry.seriesTitle}><strong>{entry.seriesTitle}</strong><span>{failures} consecutive {failures === 1 ? "failure" : "failures"} · {trial}</span></div>; })}</div>
      </div> : <div className="protection-row is-healthy" role="status"><span className="protection-badge">Healthy</span><div className="protection-copy"><strong>All series available</strong><span>No circuit breakers are limiting translation.</span></div></div>}
    </div>
  </section>;
}

export function RecoveryDiagnostics({ diagnostics = {} }: { diagnostics?: DataRow }) {
  const donors = diagnostics.donors || {}, repairs = diagnostics.repairs || {}, admissions = diagnostics.retryAdmissions || {}, provider = diagnostics.providerHealth || {};
  const rows: Array<[string, unknown]> = [["Donor candidates selected", donors.selected], ["Donors rejected by validation", donors.current_validation_failed], ["Retry plans examined", admissions.examined], ["Retry submissions", admissions.submitted], ["No-progress admissions", admissions.no_progress], ["Queued repair jobs", repairs.queued], ["Restart-persisted repairs", repairs.persisted_for_restart], ["Malformed provider responses", provider.malformed_response]];
  const maintenance = diagnostics.maintenance;
  const hasActivity = rows.some(([, value]) => numberValue(value) > 0) || Boolean(maintenance);
  const [open, setOpen] = useState(hasActivity);
  const note = maintenance ? `Latest maintenance: ${operationLabel(maintenance.operation)} · ${labelForState(maintenance.state)}` : "No persisted maintenance run yet.";
  return <details className="panel diagnostics-panel" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}><summary><span>Recovery reliability</span><small>{note}</small></summary>
    <div className="maintenance-grid">{rows.map(([label, value]) => <div className="maintenance-item" key={label}><span className="maintenance-label">{label}</span><strong className="maintenance-value">{numberValue(value).toLocaleString()}</strong></div>)}</div>
  </details>;
}

const outcomeLabels: Array<[string, string, string]> = [["Failed", "failed", "tone-danger"], ["Timed out", "timed_out", "tone-danger"], ["Waiting for retry", "waiting_retry", "tone-warning"], ["Circuit protected", "series_protected", "tone-warning"], ["Missing source", "missing_source", "tone-warning"], ["Deferred", "deferred", "tone-warning"], ["Quarantined", "quarantined", "tone-danger"]];
const maintenanceLabels: Record<string, string> = { formatted: "Formatted", repaired: "Repaired", quarantined: "Quarantined", deleted: "Deleted", undersized: "Undersized", pruned: "Pruned", source_less_warnings: "Source-less warnings", repeat_quarantines: "Repeat quarantines", cycle_suppressions: "Same-cycle suppressions", variant_outputs: "Variant outputs", failures: "Failures" };

export function RollingOutcomes({ history = {} }: { history?: Record<string, DataRow> }) {
  return <section className="panel"><PanelHeader title="Rolling outcomes" note="Repaired is included within accepted." /><div className="rolling-grid">{Object.entries(history).map(([window, values]) => <article className="window-card" key={window}><h3 className="window-title">{window}</h3>
    <div className="accepted-summary">{numberValue(values.accepted).toLocaleString()} accepted<small>({numberValue(values.repaired).toLocaleString()} repaired)</small></div>
    {outcomeLabels.map(([label, key, tone]) => { const count = numberValue(values[key]); return <div className={`outcome-line ${tone} ${count ? "" : "is-zero"}`} key={key}><span>{label}</span><strong>{count.toLocaleString()}</strong></div>; })}
  </article>)}</div></section>;
}

export function RollingMaintenance({ history = {} }: { history?: Record<string, DataRow> }) {
  return <section className="panel"><PanelHeader title="Rolling maintenance" note="Maintenance totals are separate from wanted-cycle outcomes." /><div className="rolling-grid">{Object.entries(history).map(([window, values]) => <article className="window-card" key={window}><h3 className="window-title">{window}</h3><div className="maintenance-window">
    {Object.entries(maintenanceLabels).map(([key, label]) => { const count = numberValue(values[key]); return <div className={`outcome-line ${key === "failures" ? "tone-danger" : ""} ${count ? "" : "is-zero"}`} key={key}><span>{label}</span><strong>{count.toLocaleString()}</strong></div>; })}
  </div></article>)}</div></section>;
}

export function LatestMaintenance({ maintenance = {} }: { maintenance?: DataRow }) {
  const lastScan = maintenance.lastScan;
  const entries = Object.entries(lastScan?.metrics || {}).filter(([, value]) => numberValue(value) > 0);
  return <section className="panel"><PanelHeader title="Latest maintenance scan" note={lastScan?.timestamp ? `Scanned ${relativeTime(lastScan.timestamp)}` : "No scan recorded"} />
    {entries.length ? <div className="maintenance-grid">{entries.map(([key, value]) => <div className="maintenance-item" key={key}><span className="maintenance-label">{maintenanceLabels[key] || key.replaceAll("_", " ")}</span><strong className="maintenance-value">{numberValue(value).toLocaleString()}</strong></div>)}</div> : <p className="empty-state">No maintenance actions in the latest scan.</p>}
  </section>;
}

export function RecentPanels({ recent, maintenance, timeZone, now }: { recent: DataRow[]; maintenance: DataRow; timeZone: string; now: number }) {
  return <><section className="panel"><PanelHeader title="Recent outcomes" note="Latest completed work" /><DataTable rows={recent} kind="recent" emptyMessage="No completed jobs recorded yet." timeZone={timeZone} now={now} /></section>
    <section className="panel"><PanelHeader title="Recent maintenance" note="Latest completed maintenance work" /><DataTable rows={maintenance.recentOutcomes || []} kind="recent" emptyMessage="No maintenance outcomes recorded yet." timeZone={timeZone} now={now} /></section></>;
}
