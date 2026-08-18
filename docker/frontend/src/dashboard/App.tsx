import { useCallback, useEffect, useRef, useState } from "react";
import { requestJson } from "../shared/api";
import { AppHeader } from "../shared/components";
import { parseTime, relativeTime } from "../shared/time";
import { labelForState } from "./format";
import { Observations } from "./Observations";
import { Diagnostics, LatestMaintenance, Overview, RecentPanels, RecoveryDiagnostics, RollingMaintenance, RollingOutcomes } from "./Sections";
import type { StatusSnapshot, WorkView } from "./types";
import { RecoveryAttention, Work, activeRetryPlans } from "./Work";

const ACTIVE_REFRESH_MS = 3_000;
const IDLE_REFRESH_MS = 20_000;
const MAX_BACKOFF_MS = 60_000;

function useClock() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1_000); return () => window.clearInterval(timer); }, []);
  return now;
}

function useStatusPolling(initial: StatusSnapshot) {
  const [snapshot, setSnapshot] = useState(initial);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [nextRefreshAt, setNextRefreshAt] = useState(0);
  const snapshotRef = useRef(initial);
  const inFlight = useRef(false);
  const failures = useRef(0);
  const timer = useRef<number | null>(null);
  const stopped = useRef(false);
  const pollRef = useRef<(manual?: boolean) => Promise<void>>(async () => undefined);

  const schedule = useCallback(() => {
    if (timer.current != null) window.clearTimeout(timer.current);
    if (stopped.current || document.hidden) return;
    const active = (snapshotRef.current.activeJobs?.length || 0) + (snapshotRef.current.maintenance?.activeJobs?.length || 0);
    const base = active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS;
    const delay = failures.current ? Math.min(MAX_BACKOFF_MS, base * (2 ** failures.current)) : base;
    setNextRefreshAt(Date.now() + delay);
    timer.current = window.setTimeout(() => void pollRef.current(false), delay);
  }, []);

  const poll = useCallback(async (_manual = false) => {
    if (inFlight.current || document.hidden) return;
    inFlight.current = true;
    if (timer.current != null) window.clearTimeout(timer.current);
    setLoading(true);
    try {
      const next = await requestJson<StatusSnapshot>("/api/status");
      if (stopped.current) return;
      snapshotRef.current = next;
      setSnapshot(next);
      setError("");
      failures.current = 0;
    } catch (requestError) {
      if (stopped.current) return;
      setError(requestError instanceof Error ? requestError.message : "Status request failed");
      failures.current += 1;
    } finally {
      inFlight.current = false;
      if (!stopped.current) { setLoading(false); schedule(); }
    }
  }, [schedule]);
  pollRef.current = poll;

  useEffect(() => {
    stopped.current = false;
    schedule();
    const visibility = () => {
      if (document.hidden) { if (timer.current != null) window.clearTimeout(timer.current); }
      else void pollRef.current(false);
    };
    document.addEventListener("visibilitychange", visibility);
    return () => { stopped.current = true; if (timer.current != null) window.clearTimeout(timer.current); document.removeEventListener("visibilitychange", visibility); };
  }, [schedule]);

  return { snapshot, error, loading, nextRefreshAt, refresh: () => void poll(true) };
}

export function DashboardApp({ initialSnapshot, timeZone = "UTC" }: { initialSnapshot: StatusSnapshot; timeZone?: string }) {
  const { snapshot, error, loading, nextRefreshAt, refresh } = useStatusPolling(initialSnapshot);
  const [workView, setWorkView] = useState<WorkView>("auto");
  const now = useClock();
  const service = snapshot.service || {};
  const cycle = snapshot.currentCycle || {};
  const maintenance = snapshot.maintenance || {};
  const active = [...(snapshot.activeJobs || []), ...(maintenance.activeJobs || [])];
  const upcoming = snapshot.upNext || [];
  const retryPlans = snapshot.retryPlans || [];
  const manualPlans = retryPlans.filter((plan) => plan.manualReview);
  const automaticRetries = activeRetryPlans(retryPlans);
  const completedCycle = Number(snapshot.completedCycle || 0);
  const generated = parseTime(snapshot.generatedAt);
  const stale = !generated || now - generated.getTime() > 30_000;
  const countdown = nextRefreshAt ? Math.max(0, Math.ceil((nextRefreshAt - now) / 1000)) : 0;
  const showRetry = () => { setWorkView("retry"); window.setTimeout(() => document.getElementById("retry-queue")?.scrollIntoView({ block: "start" })); };

  return <main id="dashboard-react">
    <div className="dashboard-shell">
      <AppHeader
        eyebrow={<><span className="status-dot" aria-hidden="true" />{labelForState(service.phase || "startup")}</>}
        title="Translation status"
        description={<><span>Cycle #{cycle.number ?? "—"}</span><span>Last updated {relativeTime(snapshot.generatedAt, now)}</span><span>{error || stale ? <span className="status-warning" role="status">Update delayed</span> : `Refresh in ${countdown}s`}</span></>}
        current="status"
        reviewCount={manualPlans.length}
        action={<button className="btn btn-primary" type="button" disabled={loading} onClick={refresh}>{loading ? "Refreshing…" : "Refresh now"}</button>}
      />
      <Overview cycle={cycle} service={service} />
      <Work activeJobs={active} upcoming={upcoming} retryPlans={retryPlans} completedCycle={completedCycle} maxAttempts={Number(snapshot.retryMaxAttempts || 0)} timeZone={timeZone} now={now} requestedView={workView} onView={setWorkView} />
      <RecoveryAttention plans={automaticRetries} completedCycle={completedCycle} manualReviewCount={manualPlans.length} onRetry={showRetry} />
      <Diagnostics timing={snapshot.timing} circuits={snapshot.circuits} />
      <RecoveryDiagnostics diagnostics={service.recoveryDiagnostics} />
      <RecentPanels recent={snapshot.recentOutcomes || []} maintenance={maintenance} timeZone={timeZone} now={now} />
      <Observations observations={snapshot.validationObservations} timeZone={timeZone} />
      <RollingOutcomes history={snapshot.history} />
      <RollingMaintenance history={maintenance.history} />
      <LatestMaintenance maintenance={maintenance} />
      <p className="footer-note">Auto-refreshes every 3 seconds while active and 20 seconds while idle · trusted LAN endpoint · no subtitle text or filesystem paths exposed</p>
    </div>
  </main>;
}
