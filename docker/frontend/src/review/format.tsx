import type { ReactNode } from "react";
import type { ReviewHistory } from "./types";

const OPERATOR_LABELS: Record<string, string> = {
  episodes: "Episode", movies: "Movie",
  whole_file_validation_failure: "Whole-file validation failed",
  copied_source: "Copied source text",
  target_unavailable: "Target file unavailable",
  target_unavailable_after_external_restoration_check: "Target file was not restored",
  validation_passed: "Validation passed", valid_with_warnings: "Valid with observations",
  "source-aware": "Source-aware", "target-only": "Target-only",
  manual_review: "Manual review", bazarr_scan: "Bazarr scan",
  queue_retry: "Queue retry", recheck: "Recheck", dismiss: "Dismiss",
  dispatched: "Dispatched", pending: "Pending", claimed: "Dispatching",
  failed: "Failed", resolved: "Resolved", queued: "Queued", invalid: "Invalid",
};

export const numberValue = (value: unknown) => Number.isFinite(Number(value)) ? Number(value) : 0;
export const label = (value: unknown) => String(value || "—").replaceAll("_", " ");
export const operatorLabel = (value: unknown) => OPERATOR_LABELS[String(value || "")] || label(value);
export const yesNo = (value: unknown) => value ? "Yes" : "No";

export function ReviewTime({ value, timeZone }: { value: unknown; timeZone: string }) {
  const parsed = new Date(numberValue(value) * 1000);
  if (Number.isNaN(parsed.getTime())) return <>Unknown</>;
  const options: Intl.DateTimeFormatOptions = { timeZone, dateStyle: "medium", timeStyle: "long" };
  let text: string;
  try { text = new Intl.DateTimeFormat(undefined, options).format(parsed); }
  catch { text = new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(parsed); }
  return <time dateTime={parsed.toISOString()} title={parsed.toISOString()}>{text}</time>;
}

const detailLabels: Record<string, string> = {
  validationResult: "Validation result", validationMode: "Validation mode",
  issueRules: "Issue rules", observationCount: "Observations",
  sourceAvailable: "Source available", targetAvailable: "Target available",
  artifactAvailable: "Artifact available", mediaAvailable: "Media available",
  scanPending: "Scan pending",
};
const codeFields = new Set(["validationResult", "validationMode", "issueRules"]);

export function CompletenessDetails({ value }: { value?: Record<string, unknown> }) {
  if (!value) return null;
  const thresholds = value.thresholds && typeof value.thresholds === "object"
    ? Object.entries(value.thresholds as Record<string, unknown>).map(([key, item]) => `${label(key)}: ${String(item)}`).join(", ")
    : "—";
  const rows: Array<[string, ReactNode]> = [
    ["Evaluated", yesNo(value.evaluated)], ["Undersized", yesNo(value.undersized)],
    ["Reason", value.reason ? operatorLabel(value.reason) : "—"],
    ["Media duration", value.mediaDurationSeconds == null ? "—" : `${String(value.mediaDurationSeconds)}s`],
    ["Subtitle bytes", String(value.subtitleBytes ?? "—")], ["Cue count", String(value.cueCount ?? "—")],
    ["Dialogue characters", String(value.dialogueChars ?? "—")], ["Cues per minute", String(value.cuesPerMinute ?? "—")],
    ["Text characters per minute", String(value.textCharsPerMinute ?? "—")], ["Bytes per minute", String(value.bytesPerMinute ?? "—")],
    ["Timeline coverage", String(value.timelineCoverage ?? "—")],
    ["Failed signals", Array.isArray(value.failedSignals) ? value.failedSignals.map(operatorLabel).join(", ") || "None" : "None"],
    ["Thresholds", thresholds],
  ];
  return <dl className="review-audit-details">{rows.map(([name, item]) => <div key={name}><dt>{name}</dt><dd>{item}</dd></div>)}</dl>;
}

export function AuditDetails({ value }: { value?: Record<string, unknown> }) {
  if (!value || !Object.keys(value).length) return null;
  const rows = Object.entries(value).filter(([key]) => key !== "completeness");
  return <>
    {rows.length > 0 && <dl className="review-audit-details">{rows.map(([key, item]) => {
      const display = Array.isArray(item)
        ? item.map((entry) => codeFields.has(key) ? operatorLabel(entry) : String(entry)).join(", ") || "None"
        : typeof item === "boolean" ? yesNo(item) : codeFields.has(key) ? operatorLabel(item) : String(item ?? "—");
      return <div key={key}><dt>{detailLabels[key] || label(key)}</dt><dd>{display}</dd></div>;
    })}</dl>}
    {value.completeness && <><h5>Completeness</h5><CompletenessDetails value={value.completeness as Record<string, unknown>} /></>}
  </>;
}

export function ActionHistory({ actions = [], count = 0, truncated = false, timeZone }: {
  actions?: ReviewHistory[]; count?: number; truncated?: boolean; timeZone: string;
}) {
  if (!actions.length) return <p className="empty-state">No manual actions recorded.</p>;
  return <>
    {truncated && <p className="section-note">Showing the latest {actions.length.toLocaleString()} of {numberValue(count).toLocaleString()} actions.</p>}
    <ol className="review-history">{actions.map((entry, index) => <li key={`${entry.createdAt}-${index}`}>
      <div><strong>{operatorLabel(entry.action)}</strong> <span className="badge badge-default">{entry.outcome || "recorded"}</span></div>
      <span><ReviewTime value={entry.createdAt} timeZone={timeZone} /></span>
      {entry.reasonCode && <div className="review-history-reason">{operatorLabel(entry.reasonCode)} <details className="technical-code"><summary>Technical code</summary><code>{entry.reasonCode}</code></details></div>}
      <AuditDetails value={entry.details} />
    </li>)}</ol>
  </>;
}

export const statusLabel = (value?: string) => ({ needs_attention: "Needs attention", manually_queued: "Manually queued", resolved: "Resolved", dismissed: "Dismissed" })[value || ""] || value || "Unknown";
export const statusTone = (value?: string) => ({ needs_attention: "badge-warning", manually_queued: "badge-accent", resolved: "badge-success", dismissed: "badge-default" })[value || ""] || "badge-default";

export const availabilityText = (reason?: string) => ({
  available: "Available inside a managed root",
  not_found: "File not found or no path is recorded",
  outside_managed_root: "Path is outside the configured managed roots",
  resolver_unavailable: "Availability could not be checked",
})[reason || ""] || "Availability is unknown";
