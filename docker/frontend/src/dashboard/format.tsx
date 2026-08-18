import { useEffect, useId, useRef, useState } from "react";
import { exactTime, formatDuration, parseTime, relativeTime } from "../shared/time";
import type { DataRow } from "./types";

export const numberValue = (value: unknown) => Number.isFinite(Number(value)) ? Number(value) : 0;

export const labelForState = (state: unknown) => {
  const clean = String(state || "unknown");
  return ({
    waiting_retry: "Waiting for retry", series_protected: "Circuit protected", missing_source: "Missing source",
    repair_queued: "Repair queued", repair_waiting_capacity: "Waiting for capacity", repairing: "Repairing",
    repair_validating: "Validating repaired file", scanning: "Scanning library", waiting_repair_completion: "Waiting for repairs",
    synchronizing: "Synchronizing Bazarr", retaining: "Applying retention", pruning: "Pruning sidecars",
    startup_wait: "Startup wait", startup_sync: "Startup synchronization", startup_cleanup: "Startup cleanup",
    cycle_work: "Cycle work", retry_recovery: "Retry recovery", repair_drain: "Repair drain",
    post_cycle_maintenance: "Post-cycle maintenance", cooldown: "Cooldown",
  } as Record<string, string>)[clean] || clean.replaceAll("_", " ");
};

export const operationLabel = (operation: unknown) => ({
  translation: "Translation", cue_repair: "Cue repair", format_repair: "Format repair",
  validation: "Validation", quarantine: "Quarantine", deletion: "Deletion",
  undersized_detection: "Undersized detection", sidecar_pruning: "Sidecar pruning",
  bazarr_sync: "Bazarr synchronization", existing_library_scan: "Existing-library scan",
  startup: "Startup", retention: "Retention",
} as Record<string, string>)[String(operation || "")] || String(operation || "Work").replaceAll("_", " ");

export const mediaDetail = (row: DataRow) => [row.episodeCode, row.episodeTitle].filter(Boolean).join(" · ");
export const detailedReason = (row: DataRow) => {
  const detail = row.failureDetails || {};
  return [row.reason, detail.category ? `Category: ${detail.category}` : null,
    detail.provider && detail.provider !== "unknown" ? `Provider: ${detail.provider}` : null,
    detail.model && detail.model !== "unknown" ? `Model: ${detail.model}` : null,
    detail.errorMessage, ...(Array.isArray(detail.events) ? detail.events.slice(-2) : [])]
    .filter(Boolean).join(" | ");
};

export function TimeValue({ value, timeZone, relative = true, className = "" }: {
  value: unknown; timeZone: string; relative?: boolean; className?: string;
}) {
  if (!parseTime(value)) return <span className="duration">—</span>;
  const exact = exactTime(value, timeZone);
  return relative
    ? <><time className={`relative-time ${className}`.trim()} dateTime={String(value)} title={exact}>{relativeTime(value)}</time><span className="time-exact">{exact}</span></>
    : <time className="time-exact-only" dateTime={String(value)}>{exact}</time>;
}

export function Media({ row }: { row: DataRow }) {
  const detail = mediaDetail(row);
  return <><span className="media-title">{row.title || "Unknown"}</span>{detail && <span className="media-detail">{detail}</span>}</>;
}

export function StatusBadge({ state, reason = "" }: { state: unknown; reason?: string }) {
  const clean = String(state || "unknown");
  const id = useId();
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const position = () => {
      const trigger = buttonRef.current, tooltip = tooltipRef.current;
      if (!trigger || !tooltip) return;
      const margin = 8, padding = 12;
      const triggerRect = trigger.getBoundingClientRect(), tooltipRect = tooltip.getBoundingClientRect();
      const above = triggerRect.top - margin - tooltipRect.height >= padding && triggerRect.bottom + margin + tooltipRect.height > window.innerHeight - padding;
      const top = above ? triggerRect.top - tooltipRect.height - margin : Math.min(triggerRect.bottom + margin, window.innerHeight - tooltipRect.height - padding);
      const idealLeft = triggerRect.left + triggerRect.width / 2 - tooltipRect.width / 2;
      const left = Math.min(Math.max(idealLeft, padding), window.innerWidth - tooltipRect.width - padding);
      tooltip.classList.toggle("is-above", above);
      tooltip.style.top = `${Math.max(padding, top)}px`;
      tooltip.style.left = `${Math.max(padding, left)}px`;
      tooltip.style.setProperty("--tooltip-arrow-left", `${Math.min(Math.max(triggerRect.left + triggerRect.width / 2 - left, 12), tooltipRect.width - 12)}px`);
    };
    position();
    window.addEventListener("resize", position);
    document.addEventListener("scroll", position, true);
    return () => { window.removeEventListener("resize", position); document.removeEventListener("scroll", position, true); };
  }, [open]);
  if (!reason) return <span className={`badge ${clean}`}>{labelForState(clean)}</span>;
  return <span className="status-with-tooltip">
    <button ref={buttonRef} className={`badge badge-tooltip-trigger ${clean}`} type="button" aria-describedby={id} aria-expanded={open}
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => { if (document.activeElement !== buttonRef.current) setOpen(false); }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onClick={() => setOpen(true)} onKeyDown={(event) => { if (event.key === "Escape") { setOpen(false); event.currentTarget.focus(); } }}>
      {labelForState(clean)}
    </button>
    <span ref={tooltipRef} className="status-tooltip" id={id} role="tooltip" hidden={!open}><strong>Reason</strong><span>{reason}</span></span>
  </span>;
}

export function Progress({ row }: { row: DataRow }) {
  const percent = Math.max(0, Math.min(100, numberValue(row.progress)));
  const total = numberValue(row.totalRepairableCues);
  const completed = numberValue(row.completedCues);
  const cue = row.currentCueNumber ?? row.currentCuePosition;
  let detail = total ? `${completed} of ${total} cues${cue != null ? `; cue ${cue}` : ""}` : "";
  if (!detail && numberValue(row.filesDiscovered)) detail = `${numberValue(row.filesChecked)} of ${numberValue(row.filesDiscovered)} files`;
  const attempt = row.currentAttempt ? `Attempt ${numberValue(row.currentAttempt)} of ${numberValue(row.maxAttempts) || "n/a"}` : "";
  const stage = ({ waiting_capacity: "Waiting for capacity", starting: "Starting repair", calling_lingarr: "Calling Lingarr", validating_candidate: "Validating returned cue", repairing: "Repairing cues", repair_validating: "Validating completed file", queued: "Queued" } as Record<string, string>)[row.repairStage] || "";
  if (!detail && !attempt && !stage) return <span className="duration">n/a</span>;
  return <div className="job-progress"><span>{[stage, detail, attempt].filter(Boolean).join(" / ")}</span><progress max={100} value={percent} aria-label={`${operationLabel(row.operation)} progress`}>{percent}%</progress></div>;
}

export function Remaining({ row, now }: { row: DataRow; now: number }) {
  if (row.estimatedSeconds == null) return <span className="duration">—</span>;
  const started = parseTime(row.startedAt);
  const elapsed = started ? Math.max(0, (now - started.getTime()) / 1000) : numberValue(row.durationSeconds);
  const remaining = numberValue(row.progress) > 0 && row.etaSeconds != null ? numberValue(row.etaSeconds) : numberValue(row.estimatedSeconds) - elapsed;
  return <span className="duration">{remaining >= 0 ? formatDuration(remaining) : `Over by ${formatDuration(Math.abs(remaining))}`}</span>;
}
