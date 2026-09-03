import { useEffect, useRef, useState } from "react";
import { ApiError, requestJson } from "../shared/api";
import type { CueReviewPayload, ReviewCue, ReviewItem } from "./types";

export function CueReview({ item, disabled, onMutation }: { item: ReviewItem; disabled: boolean; onMutation: (pending: boolean, message?: string) => void }) {
  const [data, setData] = useState<CueReviewPayload | null>(null);
  const [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [remember, setRemember] = useState(false), [error, setError] = useState("");
  const [loading, setLoading] = useState(true), [pending, setPending] = useState(false), [stale, setStale] = useState(false);
  const mutation = useRef(false), status = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    void requestJson<CueReviewPayload>(`/api/manual-reviews/${item.id}/cues?page=${page}&pageSize=1`, {}, controller.signal)
      .then((value) => { if (!controller.signal.aborted) { setData(value); setStale(false); setRemember(Boolean(value.items[0]?.rememberPhrase)); } })
      .catch((reason) => { if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : "Review evidence is unavailable."); setStale(true); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [item.id, item.updatedAt, page, revision]);

  const act = async (action: string, cue?: ReviewCue, approvalId?: number) => {
    if (!data || disabled || stale || loading || mutation.current) return;
    mutation.current = true; setPending(true); setError(""); onMutation(true);
    try {
      const cueEvidence = cue ? { cueNumber: cue.cueNumber, targetCueHash: cue.targetCueHash } : {};
      const decisionEvidence = { expectedUpdatedAt: data.expectedUpdatedAt, decisionRevision: data.decisionRevision, sourceHash: data.sourceHash, candidateHash: data.candidateHash };
      const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}_${Math.random().toString(36).slice(2)}`;
      const body = action === "revoke_name" ? { action, expectedUpdatedAt: data.expectedUpdatedAt, approvalRevision: data.approvalRevision, approvalId }
        : { action, requestId, ...decisionEvidence, ...cueEvidence, ...(action === "approve_cue" ? { rememberPhrase: remember } : {}) };
      const result = await requestJson<{ outcome?: string }>(`/api/manual-reviews/${item.id}/actions`, { method: "POST", headers: { "Content-Type": "application/json", "X-Bazarr-Autotranslate-Action": "manual-review" }, body: JSON.stringify(body) });
      const message = action === "finish_review" ? "Review finished. Selected cue recovery is queued." : action === "revoke_name" ? "Remembered phrase removed." : action === "clear_cue_decision" ? "Decision cleared." : "Decision saved.";
      onMutation(false, message); setRevision((value) => value + 1); if (result.outcome === "queued") setStale(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Decision could not be saved.");
      if (reason instanceof ApiError && reason.status === 409) setStale(true); onMutation(false);
    } finally { mutation.current = false; setPending(false); status.current?.focus(); }
  };

  const cue = data?.items?.[0], blocked = disabled || pending || loading || stale || !data?.actionsEnabled;
  const copied = cue?.rules?.some((rule) => rule === "copied_source" || rule === "ambiguous_copied_source");
  return <section className="cue-review" aria-label="Cue review workspace" aria-busy={loading || pending}>
    <div className="cue-review-heading"><div><p className="eyebrow">Review cues</p><h3>{item.media?.episodeCode || "Media item"} · {data?.sourceLanguage || "—"} → {data?.targetLanguage || item.targetLanguage || "—"}</h3></div>{data?.pagination && <span className="badge badge-warning">Issue {page} of {data.pagination.total}</span>}</div>
    <p ref={status} tabIndex={-1} role={error ? "alert" : "status"}>{error || (loading ? "Loading comparison…" : pending ? "Saving decision…" : "Approve the cue, try it again, or leave it undecided.")}</p>
    <button className="btn btn-sm btn-secondary" type="button" disabled={loading || pending} onClick={() => setRevision((value) => value + 1)}>Refresh evidence</button>
    {data?.unavailableReason && <p className="review-notice">{data.unavailableReason} Use the file-level recovery actions above.</p>}
    {data?.fileFindings?.map((finding) => <p className="review-notice" key={finding.code}><strong>File needs recovery:</strong> {finding.reason}</p>)}
    {cue && <article className="cue-comparison"><header><h4>Cue {cue.cueNumber}</h4><time>{cue.timestamp}</time></header>
      <ul className="cue-findings">{cue.rules.map((rule) => <li key={rule}><code>{rule}</code></li>)}</ul><p>{cue.reason}</p>
      <div className="cue-text-grid"><div><h5>Original ({data.sourceLanguage})</h5><p className="cue-text">{cue.sourceText}</p></div><div><h5>Translation ({data.targetLanguage})</h5><p className="cue-text">{cue.targetText}</p></div></div>
      <details><summary>Adjacent dialogue</summary>{cue.context.map((context) => <div className="cue-text-grid" key={context.cueNumber}><div><strong>Original · cue {context.cueNumber}</strong><p className="cue-text">{context.sourceText}</p></div><div><strong>Translation · cue {context.cueNumber}</strong><p className="cue-text">{context.targetText}</p></div></div>)}</details>
      {copied && <label className="remember-choice"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} disabled={blocked || cue.decision === "retry"} /> Remember this exact phrase for {data.scope.startsWith("sonarr:") ? "this series" : "this item"} ({data.scope}), {data.sourceLanguage} → {data.targetLanguage}</label>}
      <div className="cue-decision-actions">{cue.decision ? <><span className={`badge ${cue.decision === "approve" ? "badge-success" : "badge-warning"}`}>{cue.decision === "approve" ? "Approved" : "Try again selected"}</span><button className="btn btn-sm btn-secondary" disabled={blocked} onClick={() => void act("clear_cue_decision", cue)}>Undo decision</button></> : <><button className="btn btn-sm btn-primary" disabled={blocked || !cue.canApproveCue} title={!cue.canApproveCue ? "This cue has a mandatory finding" : undefined} onClick={() => void act("approve_cue", cue)}>Approve cue</button><button className="btn btn-sm btn-secondary" disabled={blocked} onClick={() => void act("retry_cue", cue)}>Try again</button></>}</div>
    </article>}
    {data?.pagination && <><nav className="review-pagination" aria-label="Cue navigation"><button className="btn btn-sm btn-secondary" disabled={blocked || page <= 1} onClick={() => setPage(page - 1)}>Previous issue</button><span>{page} of {data.pagination.total}</span><button className="btn btn-sm btn-secondary" disabled={blocked || page >= data.pagination.total} onClick={() => setPage(page + 1)}>Next issue</button></nav>
      <div className="review-finish"><p><strong>{data.decisionCounts?.approved || 0}</strong> approved · <strong>{data.decisionCounts?.retry || 0}</strong> try again · <strong>{data.decisionCounts?.undecided ?? data.pagination.total}</strong> undecided</p><button className="btn btn-primary" disabled={blocked || Boolean(data.fileFindings?.length) || (data.decisionCounts?.undecided ?? data.pagination.total) > 0} onClick={() => void act("finish_review")}>Finish review</button></div>
      <details><summary>Remembered phrases</summary>{!data.approvals.length && <p>No remembered phrases.</p>}{data.approvals.map((approval) => <div className="remembered-phrase" key={approval.id}><span>{approval.sourceText} → {approval.targetText}</span><button className="btn btn-sm btn-secondary" disabled={disabled || pending} onClick={() => void act("revoke_name", undefined, approval.id)}>Forget approval</button></div>)}</details></>}
  </section>;
}
