import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import {
  loadAll,
  liveList,
  trending,
  newest,
  archivedList,
  languages,
  isFlagged,
} from "../src/lib/data.ts";

const fixtures = fileURLToPath(new URL("./fixtures", import.meta.url));
const all = () => loadAll({ dataDir: fixtures, statsDir: `${fixtures}/stats` });
const names = (list) => list.map((e) => e.repo);

test("hidden never present", () => {
  const { entries } = all();
  assert.ok(entries.length > 0);
  assert.ok(!names(entries).includes("secret/hidden-thing"));
  for (const list of [
    liveList(entries),
    trending(entries),
    newest(entries),
    archivedList(entries),
  ]) {
    assert.ok(!names(list).includes("secret/hidden-thing"));
  }
});

test("flagged excluded from trending", () => {
  const list = trending(all().entries);
  assert.ok(list.every((e) => !isFlagged(e)));
  assert.ok(!names(list).includes("quiet-co/tiny-queue"), "flagged by status");
  assert.ok(
    !names(list).includes("burst/likes-spike"),
    "flagged by likes.json",
  );
});

test("trending keeps only positive 7-day deltas, biggest first", () => {
  assert.deepEqual(names(trending(all().entries)), [
    "acme/fast-cli",
    "Pixel-Forge/shader-kit",
  ]);
});

test("sort by likes then stars then name", () => {
  assert.deepEqual(names(liveList(all().entries)), [
    "acme/fast-cli",
    "zz-tie/same-likes",
    "quiet-co/tiny-queue",
    "Pixel-Forge/shader-kit",
    "burst/likes-spike",
    "future/fresh-entry",
    "evil/hostile-stats",
    "fresh/no-stats-yet",
  ]);
});

test("live lists show live, warned and flagged only", () => {
  const live = liveList(all().entries);
  assert.ok(
    live.every((e) => ["live", "warned", "flagged"].includes(e.status)),
  );
  assert.ok(isFlagged(live.find((e) => e.repo === "burst/likes-spike")));
});

test("archived and superseded appear only in the archive list", () => {
  const { entries } = all();
  assert.deepEqual(names(archivedList(entries)).sort(), [
    "old/archived-tool",
    "old/replaced-tool",
  ]);
  for (const list of [liveList(entries), trending(entries), newest(entries)]) {
    assert.ok(!names(list).some((n) => n.startsWith("old/")));
  }
});

test("new sorts by submission time, newest first", () => {
  assert.deepEqual(names(newest(all().entries)).slice(0, 2), [
    "future/fresh-entry",
    "fresh/no-stats-yet",
  ]);
});

test("stats are merged by lowercased repo key", () => {
  const e = liveList(all().entries).find(
    (x) => x.repo === "Pixel-Forge/shader-kit",
  );
  assert.equal(e.likes, 90);
  assert.equal(e.stars, 800);
  assert.equal(e.rank, 4);
  assert.equal(e.rank_delta_7d, -1);
  assert.equal(
    e.discussion_url,
    "https://github.com/devstreamhq/devstream/discussions/102",
  );
});

test("an entry with no stats gets zero likes and no meta", () => {
  const e = all().entries.find((x) => x.repo === "fresh/no-stats-yet");
  assert.equal(e.likes, 0);
  assert.equal(e.delta_7d, 0);
  assert.equal(e.stars, null);
  assert.equal(e.language, null);
  assert.equal(e.avatar_url, null);
  assert.equal(e.discussion_url, null);
});

test("missing stats files are treated as empty", () => {
  const { entries } = loadAll({
    dataDir: fixtures,
    statsDir: `${fixtures}/does-not-exist`,
  });
  assert.equal(liveList(entries).length, 8);
  assert.ok(entries.every((e) => e.likes === 0 && e.rank === null));
});

test("languages come from meta, deduplicated and sorted", () => {
  assert.deepEqual(languages(liveList(all().entries)), [
    "Go",
    "Rust",
    "TypeScript",
  ]);
});

test("displayed rank is the Top position, so a hidden entry leaves no gap", () => {
  const live = liveList(all().entries);
  assert.deepEqual(
    live.map((e) => e.rank),
    live.map((_, i) => i + 1),
  );
  assert.equal(live.find((e) => e.repo === "zz-tie/same-likes").rank, 2);
});

test("hostile stats values are dropped, not passed through", () => {
  const e = all().entries.find((x) => x.repo === "evil/hostile-stats");
  assert.equal(e.likes, 0);
  assert.equal(e.delta_7d, 0);
  assert.equal(e.rank_delta_7d, 0);
  assert.equal(e.flagged, false);
  assert.equal(e.stars, null);
  assert.equal(e.avatar_url, null);
  assert.equal(e.language_color, null);
  assert.equal(e.pushed_at, null);
  assert.equal(e.discussion_url, null);
});

test("avatars must be https on avatars.githubusercontent.com", () => {
  const { entries } = all();
  assert.equal(
    entries.find((x) => x.repo === "burst/likes-spike").avatar_url,
    null,
  );
  assert.equal(
    entries.find((x) => x.repo === "acme/fast-cli").avatar_url,
    "https://avatars.githubusercontent.com/u/1?v=4",
  );
});

test("a Discussion link needs a positive integer", () => {
  const { entries } = all();
  assert.equal(
    entries.find((x) => x.repo === "zz-tie/same-likes").discussion_url,
    null,
  );
});

const mk = (repo, o) => ({
  repo,
  status: "live",
  flagged: false,
  added_by_seed: false,
  submitted_at: "2026-09-01T00:00:00Z",
  likes: 1,
  stars: 0,
  delta_7d: 0,
  ...o,
});
const NOW = Date.parse("2026-11-05T12:00:00Z");

test("trending needs delta >= 5 and age or seed, and no flag", () => {
  const list = [
    mk("new/community", { delta_7d: 20, submitted_at: "2026-11-03T00:00:00Z" }),
    mk("new/seed", {
      delta_7d: 6,
      submitted_at: "2026-11-03T00:00:00Z",
      added_by_seed: true,
    }),
    mk("old/four", { delta_7d: 4 }),
    mk("old/five", { delta_7d: 5 }),
    mk("old/flagged", { delta_7d: 50, flagged: true }),
  ];
  assert.deepEqual(names(trending(list, NOW)), ["new/seed", "old/five"]);
  assert.equal(liveList(list).length, 5, "still listed and ranked");
});

test("trending age: exactly 7 days old qualifies", () => {
  const e = mk("edge/seven", {
    delta_7d: 9,
    submitted_at: "2026-10-29T12:00:00Z",
  });
  assert.deepEqual(names(trending([e], NOW)), ["edge/seven"]);
});
