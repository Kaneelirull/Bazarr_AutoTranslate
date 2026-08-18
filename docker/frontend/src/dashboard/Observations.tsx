import { useMemo, useState } from "react";
import { labelForState, Media, TimeValue } from "./format";
import { PanelHeader } from "./Sections";
import type { DataRow } from "./types";

const classificationLabel = (value: unknown) => ({ likely_invariant: "Likely invariant", ambiguous: "Ambiguous" } as Record<string, string>)[String(value || "")] || labelForState(value);
const observationId = (row: DataRow) => [row.itemType, row.itemId, row.targetLanguage, row.cueNumber, row.classification, row.timestamp].map((value) => String(value ?? "")).join(":");

function Evidence({ evidence = {} }: { evidence?: DataRow }) {
  const confidence = (value: unknown) => value == null ? "n/a" : Number(value).toFixed(3);
  const rows: Array<[string, unknown]> = [["Similarity", confidence(evidence.similarity)], ["Exact normalized copy", evidence.exactNormalizedCopy ? "Yes" : "No"], ["Token count", evidence.tokenCount ?? "n/a"], ["Token shape", labelForState(evidence.tokenShape)], ["Model markers", evidence.modelMarkerCount ?? 0], ["Cue language", evidence.cueLanguage || "Unknown"], ["Cue language confidence", confidence(evidence.cueLanguageConfidence)], ["Whole-target confidence", confidence(evidence.wholeTargetConfidence)], ["Context confidence", confidence(evidence.contextConfidence)]];
  return <dl className="observation-evidence">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{String(value)}</dd></div>)}</dl>;
}

export function Observations({ observations = [], timeZone }: { observations?: DataRow[]; timeZone: string }) {
  const [search, setSearch] = useState("");
  const [classification, setClassification] = useState("");
  const [language, setLanguage] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const languages = useMemo(() => [...new Set(observations.map((row) => row.targetLanguage as string).filter(Boolean))].sort(), [observations]);
  const filtered = observations.filter((row) => {
    if (classification && row.classification !== classification) return false;
    if (language && row.targetLanguage !== language) return false;
    if (!search.trim()) return true;
    return [row.title, row.episodeCode, row.episodeTitle, row.itemType, row.targetLanguage, row.classification, row.reason, row.cueNumber].filter((value) => value != null).join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
  });
  return <section className="panel observation-panel"><PanelHeader title="Validation observations" note="Latest 20 suppressed copied-source decisions" />
    <form className="observation-filters" onSubmit={(event) => event.preventDefault()}>
      <label>Search <input type="search" maxLength={100} value={search} placeholder="Media, cue, or decision" onChange={(event) => setSearch(event.target.value)} /></label>
      <label>Classification <select value={classification} onChange={(event) => setClassification(event.target.value)}><option value="">All classifications</option><option value="likely_invariant">Likely invariant</option><option value="ambiguous">Ambiguous</option></select></label>
      <label>Language <select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="">All languages</option>{languages.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
    </form>
    {!observations.length ? <p className="empty-state">No copied-source repairs were suppressed.</p> : !filtered.length ? <p className="empty-state">No observations match these filters.</p> : <div className="table-wrap"><table className="data-table observation-table"><thead><tr><th scope="col">Media</th><th scope="col">Type</th><th scope="col">Language</th><th scope="col">Cue</th><th scope="col">Decision</th><th scope="col">Classification</th><th scope="col">Evidence</th><th scope="col">Observed</th></tr></thead><tbody>
      {filtered.map((row) => { const id = observationId(row), open = expanded.has(id); return <tr key={id}><td className="cell-media" data-label="Media"><Media row={row} /></td><td data-label="Type">{row.itemType === "movies" ? "Movie" : row.itemType === "episodes" ? "Episode" : "—"}</td><td data-label="Language">{row.targetLanguage || "—"}</td><td data-label="Cue">{row.cueNumber ?? "—"}</td><td data-label="Decision"><span className="badge badge-warning">Repair skipped</span></td><td data-label="Classification">{classificationLabel(row.classification)}</td><td data-label="Evidence"><details className="observation-details" open={open} onToggle={(event) => setExpanded((current) => { const next = new Set(current); if (event.currentTarget.open) next.add(id); else next.delete(id); return next; })}><summary>View evidence</summary><p>{row.reason || "Copied-source repair was suppressed."}</p><Evidence evidence={row.evidence} /></details></td><td data-label="Observed"><TimeValue value={row.timestamp} timeZone={timeZone} /></td></tr>; })}
    </tbody></table></div>}
  </section>;
}
