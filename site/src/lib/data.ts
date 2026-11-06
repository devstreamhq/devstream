import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

export type Status =
  "live" | "warned" | "flagged" | "hidden" | "archived" | "superseded";

export interface Entry {
  repo: string;
  category: string;
  description: string;
  submitted_by: string;
  submitted_at: string;
  issue: number;
  status: Status;
  warned_at: string | null;
  superseded_by: string | null;
  added_by_seed: boolean;
}

export interface SiteEntry extends Entry {
  likes: number;
  delta_7d: number;
  rank: number | null;
  rank_delta_7d: number;
  flagged: boolean;
  stars: number | null;
  language: string | null;
  language_color: string | null;
  pushed_at: string | null;
  avatar_url: string | null;
  discussion_url: string | null;
}

export interface Category {
  slug: string;
  name: string;
}

const DISCUSSIONS = "https://github.com/devstreamhq/devstream/discussions/";
const LIVE = new Set<Status>(["live", "warned", "flagged"]);
const GONE = new Set<Status>(["archived", "superseded"]);

// Mirrors normalize_repo in scripts/devstream/entries.py: stats keys are written with it.
export const normalizeRepo = (s: string) =>
  s
    .trim()
    .toLowerCase()
    .replace(/\/+$/, "")
    .replace(/^https:\/\/github\.com\//, "")
    .replace(/\/+$/, "")
    .replace(/\.git$/, "");

function readJson<T>(path: string, fallback: T): T {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as T;
  } catch (err) {
    // A stats branch with no likes.json yet, or local dev without stats-data, is a normal state.
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return fallback;
    throw err;
  }
}

// The stats branch is bot-written but still input: anything off-shape falls back to "no data".
const COLOR = /^#[0-9a-f]{3,8}$/i;
const finite = (v: unknown) =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const date = (v: unknown) =>
  typeof v === "string" && !Number.isNaN(Date.parse(v)) ? v : null;
function avatar(v: unknown) {
  try {
    const u = new URL(String(v));
    return u.protocol === "https:" && u.host === "avatars.githubusercontent.com"
      ? u.href
      : null;
  } catch {
    return null;
  }
}

export function loadAll(opts: { dataDir?: string; statsDir?: string } = {}) {
  const dataDir = resolve(opts.dataDir ?? process.env.DATA_DIR ?? "../data");
  const statsDir = resolve(
    opts.statsDir ?? process.env.STATS_DIR ?? "../stats-data",
  );
  const { entries } = readJson<{ entries: Entry[] }>(
    join(dataDir, "entries.json"),
    { entries: [] },
  );
  const categories = readJson<Category[]>(join(dataDir, "categories.json"), []);
  const likes = readJson<Record<string, any>>(join(statsDir, "likes.json"), {});
  const meta = readJson<Record<string, any>>(join(statsDir, "meta.json"), {});
  const discussions = readJson<Record<string, unknown>>(
    join(statsDir, "discussions.json"),
    {},
  );

  const merged: SiteEntry[] = entries
    .filter((e) => e.status !== "hidden")
    .map((e) => {
      const k = normalizeRepo(e.repo);
      const l = likes[k] ?? {};
      const m = meta[k] ?? {};
      const n = discussions[k];
      return {
        ...e,
        likes: finite(l.likes) ?? 0,
        delta_7d: finite(l.delta_7d) ?? 0,
        // Set by liveList from the Top order; the stats rank can be stale or count hidden entries.
        rank: null,
        rank_delta_7d: finite(l.rank_delta_7d) ?? 0,
        flagged: l.flagged === true,
        stars: finite(m.stars),
        language: typeof m.language === "string" ? m.language : null,
        language_color: COLOR.test(m.language_color) ? m.language_color : null,
        pushed_at: date(m.pushed_at),
        avatar_url: avatar(m.avatar_url),
        discussion_url: Number.isInteger(n) && n > 0 ? DISCUSSIONS + n : null,
      };
    });
  return { entries: merged, categories };
}

export async function getEntries(): Promise<SiteEntry[]> {
  return loadAll().entries;
}

export const isFlagged = (e: SiteEntry) => e.flagged || e.status === "flagged";

const byTop = (a: SiteEntry, b: SiteEntry) =>
  b.likes - a.likes ||
  (b.stars ?? 0) - (a.stars ?? 0) ||
  (normalizeRepo(a.repo) < normalizeRepo(b.repo)
    ? -1
    : normalizeRepo(a.repo) > normalizeRepo(b.repo)
      ? 1
      : 0);

export const liveList = (all: SiteEntry[]) =>
  all
    .filter((e) => LIVE.has(e.status))
    .sort(byTop)
    .map((e, i) => ({ ...e, rank: i + 1 }));

// Trending needs a real climb, and a listing old enough (or curated) that a fresh account can't launch itself onto it.
export const TRENDING_MIN_DELTA = 5;
const TRENDING_MIN_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export const trending = (all: SiteEntry[], now = Date.now()) =>
  liveList(all)
    .filter(
      (e) =>
        !isFlagged(e) &&
        e.delta_7d >= TRENDING_MIN_DELTA &&
        (e.added_by_seed ||
          now - Date.parse(e.submitted_at) >= TRENDING_MIN_AGE_MS),
    )
    .sort((a, b) => b.delta_7d - a.delta_7d);

export const newest = (all: SiteEntry[]) =>
  liveList(all).sort(
    (a, b) => Date.parse(b.submitted_at) - Date.parse(a.submitted_at),
  );

export const archivedList = (all: SiteEntry[]) =>
  all.filter((e) => GONE.has(e.status)).sort(byTop);

export const languages = (list: SiteEntry[]) =>
  [
    ...new Set(list.map((e) => e.language).filter((l): l is string => !!l)),
  ].sort();
