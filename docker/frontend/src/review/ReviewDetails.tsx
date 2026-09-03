import { ActionHistory, availabilityText, CompletenessDetails, numberValue, operatorLabel } from "./format";
import type { ReviewItem } from "./types";
import { CueReview } from "./CueReview";

export function ReviewDetails({ item, timeZone, disabled = false, onMutation = () => {} }: {
  item: ReviewItem; timeZone: string;
  disabled?: boolean; onMutation?: (pending: boolean, message?: string) => void;
}) {
  const paths: Array<[string, string | undefined, boolean | undefined, string | undefined]> = [
    ["Source", item.sourceRelativePath, item.sourceAvailable, item.sourceAvailabilityReason],
    ["Target", item.targetRelativePath, item.targetAvailable, item.targetAvailabilityReason],
    ["Recovery artifact", item.artifactRelativePath, item.artifactAvailable, item.artifactAvailabilityReason],
    ["Media", item.mediaRelativePath, item.mediaAvailable, item.mediaAvailabilityReason],
  ];
  return <>
    <CueReview item={item} disabled={disabled} onMutation={onMutation} />
    <details className="review-details">
    <summary>Recovery details</summary>
    <dl className="review-detail-grid">
      <div><dt>Failure class</dt><dd>{operatorLabel(item.failureClass)}</dd></div>
      <div><dt>Retries completed</dt><dd>{numberValue(item.attemptCount).toLocaleString()}</dd></div>
      <div><dt>Validation rules</dt><dd>{item.failureRules?.map(operatorLabel).join(", ") || "—"}</dd></div>
      <div><dt>Recovered cues</dt><dd>{numberValue(item.recovery?.validRecoveredCueCount).toLocaleString()}</dd></div>
      <div><dt>Unresolved cues</dt><dd>{numberValue(item.recovery?.unresolvedCueCount).toLocaleString()}</dd></div>
      <div><dt>Recovery stage</dt><dd>{operatorLabel(item.recovery?.latestRecoveryStage)}</dd></div>
      <div><dt>Validation outcome</dt><dd>{operatorLabel(item.validationFeedback?.validationResult || item.validationFeedback?.outcome || "Not rechecked")}</dd></div>
      <div><dt>Validation reason</dt><dd>{operatorLabel(item.validationFeedback?.reasonCode)}</dd></div>
      <div><dt>Bazarr scan</dt><dd>{item.scanPending ? "Pending delivery" : operatorLabel(item.scanState || "Not requested")}</dd></div>
      <div className="review-detail-wide"><dt>Last reason</dt><dd>{item.lastReason || "—"}</dd></div>
    </dl>
    <details className="technical-code review-technical-codes"><summary>Technical codes</summary>
      <dl className="review-audit-details">
        <div><dt>Failure class</dt><dd><code>{item.failureClass || ""}</code></dd></div>
        <div><dt>Validation rules</dt><dd><code>{item.failureRules?.join(", ") || ""}</code></dd></div>
        <div><dt>Recovery stage</dt><dd><code>{item.recovery?.latestRecoveryStage || ""}</code></dd></div>
        <div><dt>Validation reason</dt><dd><code>{item.validationFeedback?.reasonCode || ""}</code></dd></div>
      </dl>
    </details>
    {item.validationFeedback?.completeness && <><h4>Completeness evidence</h4><CompletenessDetails value={item.validationFeedback.completeness} /></>}
    <h4>Managed paths</h4>
    <dl className="review-paths">{paths.map(([name, path, available, reason]) => <div key={name}>
      <dt>{name}</dt><dd><span className={`badge ${available ? "badge-success" : "badge-warning"}`}>{available ? "Available" : "Unavailable"}</span>
        <span className="review-availability-reason">{availabilityText(reason)}</span>
        {path && <code className="review-managed-path">{path}</code>}
      </dd>
    </div>)}</dl>
    <h4>Action history</h4>
    <ActionHistory actions={item.actions} count={item.actionCount} truncated={item.actionsTruncated} timeZone={timeZone} />
    </details>
  </>;
}
