export function parseTime(value: unknown): Date | null {
  if (!value) return null;
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? null : date;
}

export function relativeTime(value: unknown, now = Date.now()): string {
  const date = parseTime(value);
  if (!date) return "—";
  const deltaSeconds = Math.round((date.getTime() - now) / 1000);
  const future = deltaSeconds > 0;
  const absolute = Math.abs(deltaSeconds);
  if (absolute < 10) return future ? "in a few seconds" : "just now";
  if (absolute < 60) return future ? `in ${absolute}s` : `${absolute}s ago`;
  const minutes = Math.round(absolute / 60);
  if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`;
  const hours = Math.round(absolute / 3600);
  if (hours < 24) return future ? `in ${hours}h` : `${hours}h ago`;
  const days = Math.round(absolute / 86400);
  return future ? `in ${days}d` : `${days}d ago`;
}

export function exactTime(value: unknown, timeZone: string): string {
  const date = parseTime(value);
  if (!date) return "—";
  const options: Intl.DateTimeFormatOptions = {
    timeZone,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  };
  try {
    return new Intl.DateTimeFormat("en-GB", options).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(date);
  }
}

export function formatDuration(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  let seconds = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  seconds -= hours * 3600;
  const minutes = Math.floor(seconds / 60);
  seconds -= minutes * 60;
  return [hours ? `${hours}h` : "", minutes || hours ? `${minutes}m` : "", `${seconds}s`]
    .filter(Boolean)
    .join(" ");
}
