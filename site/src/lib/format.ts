import octicons from "@primer/octicons";

export const icon = (name: keyof typeof octicons, cls = "") =>
  octicons[name].toSVG({ "aria-hidden": "true", class: `oc ${cls}`.trim() });

export const num = (n: number) => n.toLocaleString("en-US");

export const compact = (n: number) =>
  n < 1000
    ? String(n)
    : n < 1e6
      ? `${+(n / 1000).toFixed(n < 1e4 ? 1 : 0)}k`
      : `${+(n / 1e6).toFixed(1)}m`;

// Display-only: data and scripts stay UTC. Short zone name gives CST or CDT.
export const centralStamp = (d: Date) =>
  new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  })
    .format(d)
    .replace(" at ", ", ");

const UNITS: [number, string][] = [
  [365 * 86400, "year"],
  [30 * 86400, "month"],
  [7 * 86400, "week"],
  [86400, "day"],
  [3600, "hour"],
  [60, "minute"],
];

// Build-time relative time; the site rebuilds every hour, so it never drifts far.
export function ago(iso: string, now = Date.now()) {
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  for (const [size, unit] of UNITS) {
    const n = Math.floor(s / size);
    if (n >= 1) return `${n} ${unit}${n > 1 ? "s" : ""} ago`;
  }
  return "just now";
}

// Week-on-week change as a percentage of last week's count; null when there was nothing to compare.
export const pct = (likes: number, delta: number) => {
  const before = likes - delta;
  return before > 0 ? Math.round((delta / before) * 100) : null;
};
