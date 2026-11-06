import { isFlagged, type SiteEntry } from "./data.ts";

// The light-theme accent ink: white badge text on it clears 5.8:1.
export const BRAND_HEX = "#c4155f";

const FONT = `-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif`;
// Presentation attributes rather than <style>, so a strict CSP on the response cannot strip the look.
const FG = "#e6edf3";
const MUTED = "#9198a1";
const ACC = "#ff7aa9";
const W = 720;
const ROW = 44;
const TOP = 76;

export const esc = (v: unknown) =>
  String(v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const clip = (s: string, n: number) =>
  s.length > n ? s.slice(0, n - 1) + "…" : s;
const num = (n: unknown) =>
  typeof n === "number" ? n.toLocaleString("en-US") : String(n);

const MARK =
  `<path d="M8.5 5.5H25.5L23.5 10.5H6.5Z" fill="url(#g)" stroke="url(#g)" stroke-width="1.2" stroke-linejoin="round"/>` +
  `<path d="M13.5 13.5H25.5L23.5 18.5H11.5Z" fill="#e6edf3" stroke="#e6edf3" stroke-width="1.2" stroke-linejoin="round"/>` +
  `<path d="M18.5 21.5H25.5L23.5 26.5H16.5Z" fill="#e6edf3" stroke="#e6edf3" stroke-width="1.2" stroke-linejoin="round"/>`;
const THUMB = `<path d="M2 7h2.5v7H2zM6 14V7l3-5c1.1 0 1.9.9 1.7 2L10.3 6H14c.8 0 1.4.8 1.2 1.6l-1.3 5.2c-.2.7-.8 1.2-1.5 1.2Z"/>`;

export function top10Svg(live: SiteEntry[]): string {
  // Same rule as Trending: a flagged entry doesn't get promoted off-site.
  const rows = live.filter((e) => !isFlagged(e)).slice(0, 10);
  const h = TOP + (rows.length ? rows.length * ROW : 64) + 20;
  const max = Math.max(1, ...rows.map((e) => +e.likes || 0));
  const body = rows.length
    ? rows
        .map((e, i) => {
          const y = TOP + i * ROW;
          const [owner, name] = e.repo.includes("/")
            ? [
                e.repo.slice(0, e.repo.indexOf("/") + 1),
                e.repo.slice(e.repo.indexOf("/") + 1),
              ]
            : ["", e.repo];
          const first = i === 0;
          const bar = Math.max(4, Math.round(((+e.likes || 0) / max) * 500));
          return (
            `<g transform="translate(0 ${esc(y)})">` +
            (first
              ? `<rect x="16" y="0" width="${W - 32}" height="${ROW - 6}" rx="8" fill="#151b23" stroke="url(#g)"/>`
              : "") +
            `<text x="48" y="24" font-size="15" font-weight="600" fill="${first ? ACC : MUTED}" text-anchor="end">${esc(e.rank ?? i + 1)}</text>` +
            `<text x="64" y="24" font-size="15" font-weight="600"><tspan fill="${MUTED}" font-weight="400">${esc(clip(owner, 24))}</tspan>${esc(clip(name, 40))}</text>` +
            `<rect x="64" y="31" width="${esc(bar)}" height="2" rx="1" fill="${first ? "url(#g)" : "#3d444d"}"/>` +
            `<g transform="translate(${W - 132} 13)" fill="${first ? ACC : MUTED}">${THUMB}</g>` +
            `<text x="${W - 32}" y="24" font-size="14" font-weight="600" fill="${first ? ACC : FG}" text-anchor="end">${esc(num(e.likes))}</text>` +
            `</g>`
          );
        })
        .join("")
    : `<text x="${W / 2}" y="${TOP + 28}" font-size="15" font-weight="600" text-anchor="middle">The list is filling up.</text>` +
      `<text x="${W / 2}" y="${TOP + 52}" font-size="13" fill="${MUTED}" text-anchor="middle">Submit a repo and it could be the first one here.</text>`;

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${h}" width="${W}" height="${h}" role="img" aria-label="DevStream top 10 repos by developer Likes">` +
    `<title>DevStream top 10 repos by developer Likes</title>` +
    `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f72585"/><stop offset=".55" stop-color="#ff5a36"/><stop offset="1" stop-color="#ffa51f"/></linearGradient></defs>` +
    `<rect x=".5" y=".5" width="${W - 1}" height="${h - 1}" rx="12" fill="#0d1117" stroke="#3d444d"/>` +
    `<g transform="translate(18 16)">${MARK}</g>` +
    `<g font-family='${FONT}' fill="${FG}">` +
    `<text x="56" y="37" font-size="16" font-weight="700">Top 10 on DevStream</text>` +
    `<text x="${W - 32}" y="37" font-size="13" fill="${MUTED}" text-anchor="end">Ranked by developer Likes</text>` +
    `<path d="M16 60.5H${W - 16}" stroke="#3d444d"/>` +
    body +
    `</g></svg>`
  );
}

export const badge = (live: SiteEntry[]) => ({
  schemaVersion: 1,
  label: "listed",
  message: `${live.length} ${live.length === 1 ? "repo" : "repos"}`,
  color: BRAND_HEX,
});

// shortcut: widths are estimated at 6.6px a character, not measured; fine for short ASCII labels.
export function flatBadge(label: string, message: string, color: string): string {
  const lw = Math.round(label.length * 6.6 + 10);
  const rw = Math.round(message.length * 6.6 + 10);
  const w = lw + rw;
  const t = (x: number, s: string) =>
    `<text x="${x}" y="14" fill="#fff" text-anchor="middle">${esc(s)}</text>`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="20" role="img" aria-label="${esc(label)}: ${esc(message)}">` +
    `<title>${esc(label)}: ${esc(message)}</title>` +
    `<clipPath id="r"><rect width="${w}" height="20" rx="3"/></clipPath>` +
    `<g clip-path="url(#r)"><rect width="${lw}" height="20" fill="#555"/><rect x="${lw}" width="${rw}" height="20" fill="${esc(color)}"/></g>` +
    `<g font-family="${esc(FONT)}" font-size="11">${t(lw / 2, label)}${t(lw + rw / 2, message)}</g></svg>`
  );
}
