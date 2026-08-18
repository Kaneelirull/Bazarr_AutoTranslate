import { Fragment, useMemo, useState } from "react";
import { DataTable } from "./DataTable";
import { labelForState, numberValue } from "./format";
import { PanelHeader } from "./Sections";
import type { DataRow, RetrySort, WorkView } from "./types";

const ACTIVE_RETRY_STATES = new Set(["repair_retry_queued", "regeneration_waiting", "regeneration_queued", "retry_in_progress"]);
const SORT_LABELS: Record<RetrySort, string> = { media: "Media", language: "Language", status: "Status", attempts: "Retries", nextAction: "Next action" };
const RETRY_BATCH = 20;
const QUEUE_BATCH = 10;

export const activeRetryPlans = (plans: DataRow[]) => plans.filter((plan) => !plan.manualReview && ACTIVE_RETRY_STATES.has(plan.state));
const planId = (plan: DataRow) => String(plan.id ?? `${plan.itemType || "media"}-${plan.itemId ?? "unknown"}-${plan.targetLanguage || "unknown"}`);
const media = (plan: DataRow) => ({ title: plan.displayTitle || `${plan.itemType || "media"} ${plan.itemId ?? "?"}`, detail: [plan.episodeCode, plan.episodeTitle].filter(Boolean).join(" - ") });

function retryState(state: unknown, plan: DataRow, completedCycle: number): [string, string] {
  const states: Record<string, [string, string]> = {
    regeneration_waiting: [numberValue(plan.eligibleCompletedCycle) > completedCycle ? (numberValue(plan.eligibleCompletedCycle) === completedCycle + 1 ? "Next cycle" : "Scheduled") : "Due now", "deferred"],
    regeneration_queued: ["Admitted", "translating"], waiting_lane: ["Waiting for lane", "queued"],
    retry_in_progress: ["Translating", "translating"], repair_retry_queued: ["Repair queued", "repairing"],
    retry_exhausted: ["Retry exhausted", "failed"], source_blocked: ["Source blocked", "failed"],
  };
  return states[String(state || "")] || [labelForState(state || "queued"), "queued"];
}

function nextAction(plan: DataRow, completedCycle: number) {
  if (plan.manualReview) return "Manual review";
  if (String(plan.lastReason || "").toLowerCase().includes("circuit")) return "Waiting for circuit";
  if (plan.runtimeState === "waiting_lane") return "Waiting for lane";
  if (plan.runtimeState === "retry_in_progress" || plan.state === "retry_in_progress") return "Translating";
  if (plan.state === "regeneration_queued") return "Admitted";
  if (plan.state === "regeneration_waiting") {
    const remaining = Math.max(0, numberValue(plan.eligibleCompletedCycle) - completedCycle);
    if (!remaining) return "Due now";
    if (plan.lastDeferralClass) return "Rescheduled after no progress";
    return remaining === 1 ? "Next cycle" : `In ${remaining} cycles`;
  }
  if (plan.state === "repair_retry_queued") return "Repair at cycle end";
  if (["retry_exhausted", "source_blocked"].includes(plan.state)) return "Manual review";
  return labelForState(plan.state || "queued");
}

function sortValue(plan: DataRow, sort: RetrySort, completedCycle: number): string | number {
  if (sort === "media") return media(plan).title;
  if (sort === "language") return plan.targetLanguage || "";
  if (sort === "status") return retryState(plan.runtimeState || plan.state, plan, completedCycle)[0];
  if (sort === "attempts") return numberValue(plan.attemptCount);
  const action = nextAction(plan, completedCycle);
  const order = action === "Due now" || action === "Admitted" || action === "Translating" ? "0" : action === "Next cycle" ? "1" : action.startsWith("In ") ? `1-${action}` : action.includes("circuit") ? "2" : `3-${action}`;
  return order;
}

function RetryQueue({ plans, completedCycle, maxAttempts, cycleJobs }: { plans: DataRow[]; completedCycle: number; maxAttempts: number; cycleJobs: DataRow[] }) {
  const [sort, setSort] = useState<RetrySort>("nextAction");
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const [visibleCount, setVisibleCount] = useState(RETRY_BATCH);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const jobsByPlan = new Map(cycleJobs.filter((job) => job.retryPlanId != null).map((job) => [String(job.retryPlanId), job]));
  const active = activeRetryPlans(plans).map((plan) => { const job = jobsByPlan.get(planId(plan)); return job ? { ...plan, runtimeState: ["translating", "validating"].includes(job.state) ? "retry_in_progress" : "waiting_lane" } : plan; });
  const sorted = useMemo(() => [...active].sort((left, right) => {
    const leftValue = sortValue(left, sort, completedCycle), rightValue = sortValue(right, sort, completedCycle);
    const comparison = typeof leftValue === "number" && typeof rightValue === "number" ? leftValue - rightValue : String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: "base" });
    return direction === "desc" ? -comparison : comparison;
  }), [active, completedCycle, direction, sort]);
  if (!active.length) return <p className="empty-state">No retry work scheduled.</p>;
  const visible = sorted.slice(0, visibleCount);
  const dueNow = active.filter((plan) => plan.state === "regeneration_waiting" && numberValue(plan.eligibleCompletedCycle) <= completedCycle).length;
  const dueNext = active.filter((plan) => plan.state === "regeneration_waiting" && numberValue(plan.eligibleCompletedCycle) === completedCycle + 1).length;
  const changeSort = (next: RetrySort) => { if (sort === next) setDirection((value) => value === "asc" ? "desc" : "asc"); else { setSort(next); setDirection("asc"); } setVisibleCount(RETRY_BATCH); };
  const header = (key: RetrySort) => <th scope="col" aria-sort={sort === key ? direction === "asc" ? "ascending" : "descending" : undefined}><button type="button" className={`retry-sort-button ${sort === key ? "is-active" : ""}`} onClick={() => changeSort(key)}>{SORT_LABELS[key]}<span aria-hidden="true">{sort === key ? direction === "asc" ? "↑" : "↓" : "↕"}</span></button></th>;
  return <><div className="retry-toolbar"><div className="retry-summary" aria-label={`${active.length} active retries, ${dueNow} due now, ${dueNext} due next cycle`}><span><strong>{active.length}</strong> active</span><span><strong>{dueNow}</strong> due now</span><span><strong>{dueNext}</strong> next cycle</span></div>
    <div className="retry-mobile-sort"><label htmlFor="retry-sort-select">Sort by</label><select id="retry-sort-select" value={sort} onChange={(event) => { setSort(event.target.value as RetrySort); setDirection("asc"); }}>{Object.entries(SORT_LABELS).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select><button type="button" className="retry-sort-direction" aria-label={`Sort ${direction === "asc" ? "descending" : "ascending"}`} onClick={() => setDirection((value) => value === "asc" ? "desc" : "asc")}><span aria-hidden="true">{direction === "asc" ? "↑" : "↓"}</span></button></div>
  </div><div className="table-wrap"><table className="data-table retry-table"><thead><tr>{header("media")}{header("language")}{header("status")}{header("attempts")}{header("nextAction")}<th scope="col">Details</th></tr></thead><tbody>
    {visible.map((plan) => { const id = planId(plan); const detailId = `retry-detail-${id.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`; const open = expanded.has(id); const identity = media(plan); const [state, tone] = retryState(plan.runtimeState || plan.state, plan, completedCycle); const fields: Array<[string, unknown]> = [["Failure class", plan.failureClass || "—"], ["Validation rules", Array.isArray(plan.rules) ? plan.rules.join(", ") : "—"], ["Last reason", plan.lastReason || "—"], ["Eligible cycle", plan.eligibleCompletedCycle ?? "—"], ["Last admitted cycle", plan.lastAdmittedCycle ?? "—"], ["No-progress count", plan.noProgressCount ?? 0], ["Last deferral", plan.lastDeferralClass || "—"], ["Archived attempts", plan.archivedAttemptCount ?? 0], ["Latest donor attempt", plan.latestDonorAttempt ?? "—"], ["Item", `${plan.itemType || "media"}:${plan.itemId ?? "—"}`], ["Final outcome", plan.finalOutcome || "—"]]; return <Fragment key={id}>
      <tr className={`retry-main-row ${open ? "has-expanded" : ""}`} key={`${id}-main`}><td className="cell-media" data-label="Media"><span className="media-title">{identity.title}</span>{identity.detail && <span className="media-detail">{identity.detail}</span>}</td><td data-label="Language">{plan.targetLanguage || "—"}</td><td data-label="Status"><span className={`badge ${tone}`}>{state}</span></td><td data-label="Retries"><span className="duration">{maxAttempts === 0 ? `${numberValue(plan.attemptCount)} retries · Unlimited` : `${numberValue(plan.attemptCount)} of ${maxAttempts} used`}</span></td><td data-label="Next action">{nextAction(plan, completedCycle)}</td><td className="cell-details" data-label="Details"><button type="button" className="retry-details-toggle" aria-expanded={open} aria-controls={detailId} onClick={() => setExpanded((current) => { const next = new Set(current); if (open) next.delete(id); else next.add(id); return next; })}><span className="retry-details-icon" aria-hidden="true" /><span className="retry-details-label">{open ? "Hide details" : "View details"}</span></button></td></tr>
      <tr className="retry-detail-row" id={detailId} hidden={!open} key={`${id}-details`}><td colSpan={6}><dl className="retry-detail-grid">{fields.map(([label, value]) => <div className={label === "Last reason" ? "retry-detail-wide" : ""} key={label}><dt>{label}</dt><dd>{String(value).replaceAll("_", " ")}</dd></div>)}</dl></td></tr>
    </Fragment>; })}
  </tbody></table><div className="retry-table-footer"><span className="retry-showing" aria-live="polite">Showing {visible.length} of {active.length}</span>{visible.length < active.length && <button type="button" className="btn btn-secondary btn-sm retry-show-more" onClick={() => setVisibleCount((value) => value + RETRY_BATCH)}>Show {Math.min(RETRY_BATCH, active.length - visible.length)} more</button>}</div></div></>;
}

export function RecoveryAttention({ plans, completedCycle, manualReviewCount, onRetry }: { plans: DataRow[]; completedCycle: number; manualReviewCount: number; onRetry: () => void }) {
  const active = activeRetryPlans(plans), dueNow = active.filter((plan) => plan.state === "regeneration_waiting" && numberValue(plan.eligibleCompletedCycle) <= completedCycle).length, dueNext = active.filter((plan) => plan.state === "regeneration_waiting" && numberValue(plan.eligibleCompletedCycle) === completedCycle + 1).length;
  return <nav className="recovery-attention" aria-label="Recovery attention"><a href="#retry-queue" onClick={(event) => { event.preventDefault(); onRetry(); }}><span>Due now</span><strong>{dueNow}</strong></a><a href="#retry-queue" onClick={(event) => { event.preventDefault(); onRetry(); }}><span>Next cycle</span><strong>{dueNext}</strong></a><a href="/review"><span>Manual review</span><strong>{manualReviewCount}</strong></a></nav>;
}

export function Work({ activeJobs, upcoming, retryPlans, completedCycle, maxAttempts, timeZone, now, requestedView, onView }: { activeJobs: DataRow[]; upcoming: DataRow[]; retryPlans: DataRow[]; completedCycle: number; maxAttempts: number; timeZone: string; now: number; requestedView: WorkView; onView: (view: WorkView) => void }) {
  const [upcomingCount, setUpcomingCount] = useState(QUEUE_BATCH);
  const retries = activeRetryPlans(retryPlans);
  const visibleView = requestedView === "auto" ? activeJobs.length ? "active" : retries.length ? "retry" : "up-next" : requestedView;
  const notes = { active: `Active now · ${activeJobs.length.toLocaleString()} in progress`, retry: `Retry queue · ${retries.length.toLocaleString()} active · persistent quarantine recovery · completed cycle ${completedCycle}`, "up-next": `Up next · ${upcoming.length.toLocaleString()} queued job${upcoming.length === 1 ? "" : "s"}` };
  const controls = <div className="work-view-switch" role="group" aria-label="Work view">{(["auto", "active", "up-next", "retry"] as WorkView[]).map((view) => <button type="button" aria-pressed={requestedView === view} onClick={() => onView(view)} key={view}>{({ auto: "Auto", active: "Active now", "up-next": "Up next", retry: "Retry queue" })[view]}</button>)}</div>;
  return <section className="panel work-panel" id="retry-queue" data-work-view={visibleView}><PanelHeader title="Work" note={notes[visibleView]} actions={controls} />
    {visibleView === "active" && <DataTable rows={activeJobs} kind="active" emptyMessage="No active translations, repairs, startup, or maintenance." timeZone={timeZone} now={now} />}
    {visibleView === "retry" && <RetryQueue plans={retryPlans} completedCycle={completedCycle} maxAttempts={maxAttempts} cycleJobs={[...activeJobs, ...upcoming]} />}
    {visibleView === "up-next" && <><DataTable rows={upcoming.slice(0, upcomingCount)} kind="upcoming" emptyMessage="No queued jobs." timeZone={timeZone} now={now} /><div className="queue-table-footer"><span className="queue-showing" aria-live="polite">Showing {Math.min(upcomingCount, upcoming.length)} of {upcoming.length}</span>{upcomingCount < upcoming.length && <button type="button" className="btn btn-secondary btn-sm up-next-show-more" onClick={() => setUpcomingCount((value) => value + QUEUE_BATCH)}>Show {Math.min(QUEUE_BATCH, upcoming.length - upcomingCount)} more</button>}</div></>}
  </section>;
}
