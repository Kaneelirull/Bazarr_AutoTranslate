import { formatDuration } from "../shared/time";
import { detailedReason, Media, operationLabel, Progress, Remaining, StatusBadge, TimeValue } from "./format";
import type { DataRow } from "./types";

type Kind = "upcoming" | "active" | "recent";
type Column = { label: string; key: string };

export function DataTable({ rows, kind, emptyMessage, timeZone, now }: {
  rows: DataRow[]; kind: Kind; emptyMessage: string; timeZone: string; now: number;
}) {
  if (!rows.length) return <p className="empty-state">{emptyMessage}</p>;
  const showType = new Set(rows.map((row) => row.itemType).filter(Boolean)).size > 1;
  const typeColumn = showType ? [{ label: "Type", key: "type" }] : [];
  const columns: Column[] = kind === "upcoming"
    ? [{ label: "Position", key: "position" }, { label: "Media", key: "media" }, ...typeColumn, { label: "Language", key: "language" }, { label: "Queued", key: "queued" }]
    : kind === "active"
      ? [{ label: "Work", key: "work" }, { label: "Media", key: "media" }, ...typeColumn, { label: "Language", key: "language" }, { label: "Status", key: "status" }, { label: "Operation", key: "operation" }, { label: "Progress", key: "progress" }, { label: "Elapsed", key: "elapsed" }, { label: "Est. total", key: "estimate" }, { label: "Remaining", key: "eta" }, { label: "Started", key: "started" }]
      : [{ label: "Work", key: "work" }, { label: "Media", key: "media" }, ...typeColumn, { label: "Language", key: "language" }, { label: "Operation", key: "operation" }, { label: "Outcome", key: "outcome" }, { label: "Duration", key: "duration" }, { label: "Attempts", key: "attempts" }, { label: "Finished", key: "finished" }];

  const cell = (row: DataRow, key: string, index: number) => {
    if (key === "position") return <span className="queue-position">#{index + 1}</span>;
    if (key === "work") { const maintenance = row.workKind === "maintenance"; return <span className={`badge ${maintenance ? "maintenance-work" : "cycle-work"}`}>{row.operation === "startup" ? "Startup" : maintenance ? "Maintenance" : "Cycle"}</span>; }
    if (key === "media") return <Media row={row} />;
    if (key === "type") return row.itemType === "movies" ? "Movie" : "Episode";
    if (key === "language") return row.targetLanguage || "—";
    if (key === "operation") return operationLabel(row.operation);
    if (key === "progress") return <Progress row={row} />;
    if (key === "status") return <StatusBadge state={row.state} reason={detailedReason(row)} />;
    if (key === "outcome") return <StatusBadge state={row.repaired && row.outcome === "accepted" ? "repaired" : row.outcome} reason={detailedReason(row)} />;
    if (key === "elapsed") { const started = new Date(row.startedAt || ""); const value = Number.isNaN(started.getTime()) ? row.durationSeconds : (now - started.getTime()) / 1000; return <span className="duration">{formatDuration(value)}</span>; }
    if (key === "duration") return <span className="duration">{formatDuration(row.durationSeconds)}</span>;
    if (key === "estimate") return <span className="duration">{formatDuration(row.estimatedSeconds)}</span>;
    if (key === "eta") return <Remaining row={row} now={now} />;
    if (key === "attempts") return row.attempts ?? "—";
    if (key === "queued") return <TimeValue value={row.queuedAt} timeZone={timeZone} />;
    if (key === "started") return <TimeValue value={row.startedAt} timeZone={timeZone} relative={false} />;
    if (key === "finished") return <TimeValue value={row.timestamp || row.finishedAt} timeZone={timeZone} />;
    return "—";
  };

  return <div className="table-wrap"><table className="data-table"><thead><tr>{columns.map((column) => <th scope="col" key={column.key}>{column.label}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={row.key || row.id || `${row.title}-${row.targetLanguage}-${index}`}>{columns.map((column) => <td className={`cell-${column.key}`} data-label={column.label} key={column.key}>{cell(row, column.key, index)}</td>)}</tr>)}</tbody>
  </table></div>;
}
