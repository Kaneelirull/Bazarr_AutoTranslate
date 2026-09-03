import { useEffect, useRef, useState } from "react";
import { ApiError, requestJson } from "../shared/api";
import type { CueReviewPayload, ReviewCue, ReviewItem } from "./types";

export function CueReview({ item, disabled, onMutation }: {
  item: ReviewItem; disabled: boolean; onMutation: (pending: boolean, message?: string) => void;
}) {
  const [data, setData] = useState<CueReviewPayload | null>(null);
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [stale, setStale] = useState(false);
  const mutation = useRef(false);
  const status = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    void requestJson<CueReviewPayload>(`/api/manual-reviews/${item.id}/cues?page=${page}`, {}, controller.signal)
      .then((result) => { if (!controller.signal.aborted) { setData(result); setStale(false); } })
      .catch((reason) => { if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : "Cues are unavailable."); setStale(true); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [item.id, item.updatedAt, page, revision]);

  const act = async (cue?: ReviewCue, approvalId?: number) => {
    if (!data || disabled || stale || loading || mutation.current) return;
    mutation.current = true; setPending(true); setError(""); onMutation(true);
    const action = cue ? "approve_name" : "revoke_name";
    try {
      await requestJson(`/api/manual-reviews/${item.id}/actions`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review" },
        body: JSON.stringify({ action, expectedUpdatedAt: data.expectedUpdatedAt, approvalRevision: data.approvalRevision,
          ...(cue ? { sourceHash: data.sourceHash, candidateHash: data.candidateHash, cueNumber: cue.cueNumber, targetCueHash: cue.targetCueHash } : { approvalId }) }),
      });
      setStale(true);
      onMutation(false, cue ? "Name remembered. Recovery is queued; the full file still needs validation." : "Name approval forgotten. Future validation will check this phrase again.");
      setRevision((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Approval failed.");
      if (reason instanceof ApiError && reason.status === 409) setStale(true);
      onMutation(false);
    } finally {
      mutation.current = false; setPending(false);
      status.current?.focus();
    }
  };
  const blocked = disabled || pending || loading || stale;
  return <section className="cue-review" aria-label="Cues needing review" aria-busy={loading || pending}>
    <h4>Cues needing review</h4>
    <p ref={status} tabIndex={-1} role={error ? "alert" : "status"}>{error || (loading ? "Loading cue comparisons…" : pending ? "Saving decision…" : "Compare the original and translation before accepting a name.")}</p>
    <button className="btn btn-sm btn-secondary" type="button" disabled={loading || pending} onClick={() => setRevision((value) => value + 1)}>Refresh cue comparison</button>
    {data && <>
      <p className="cue-scope">Remember only this exact phrase for {data.scope.startsWith("sonarr:") ? "this series" : "this item"}: {item.media?.title || data.scope} ({data.scope}), {data.sourceLanguage} → {data.targetLanguage}.</p>
      {data.unavailableReason && <p role="status">{data.unavailableReason} Remembered approvals can still be forgotten.</p>}
      {!data.items?.length && !loading && data.candidateAvailable !== false && <p>No unresolved cue findings in this candidate.</p>}
      {data.items?.map((cue) => <article className="cue-comparison" key={cue.cueNumber}>
        <h5>Cue {cue.cueNumber} · {cue.timestamp}</h5>
        <p>{cue.reason}</p>
        <div className="cue-text-grid"><div><h6>Original ({data.sourceLanguage})</h6><p className="cue-text">{cue.sourceText}</p></div><div><h6>Translation ({data.targetLanguage})</h6><p className="cue-text">{cue.targetText}</p></div></div>
        <details><summary>Adjacent cues</summary>{cue.context.map((context) => <div className="cue-text-grid" key={context.cueNumber}><div><strong>Original · cue {context.cueNumber}</strong><p className="cue-text">{context.sourceText}</p></div><div><strong>Translation · cue {context.cueNumber}</strong><p className="cue-text">{context.targetText}</p></div></div>)}</details>
        {cue.canApproveName && <button className="btn btn-sm btn-primary" type="button" disabled={blocked || !data.actionsEnabled} onClick={() => void act(cue)}>Accept as name and remember</button>}
      </article>)}
      <nav className="review-pagination" aria-label="Cue pages"><button className="btn btn-sm btn-secondary" disabled={blocked || page <= 1} onClick={() => setPage(page - 1)}>Previous cues</button><span>Page {page} · {data.pagination?.total || 0} cues</span><button className="btn btn-sm btn-secondary" disabled={blocked || page * data.pagination.pageSize >= data.pagination.total} onClick={() => setPage(page + 1)}>Next cues</button></nav>
      <h4>Remembered names for this scope</h4>
      {!data.approvals?.length && <p>No remembered names.</p>}
      {data.approvals?.map((approval) => <div className="cue-comparison" key={approval.id}><p className="cue-text">{approval.sourceText} → {approval.targetText}</p><button className="btn btn-sm btn-secondary" disabled={blocked} onClick={() => void act(undefined, approval.id)}>Forget approval</button></div>)}
    </>}
  </section>;
}
