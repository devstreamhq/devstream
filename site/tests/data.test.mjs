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
  assert.equal(newest(all().entries)[0].repo, "fresh/no-stats-yet");
});

test("stats are merged by lowercased repo key", () => {
  const e = all().entries.find((x) => x.repo === "Pixel-Forge/shader-kit");
  assert.equal(e.likes, 90);
  assert.equal(e.stars, 800);
  assert.equal(e.rank, 4);
  assert.equal(e.rank_delta_7d, -1);
  assert.equal(
    e.discussion_url,
    "https://github.com/devstreamhq/devstream/discussions/102",
  );
});

test("an entry with no stats gets zero likes, no rank and no meta", () => {
  const e = all().entries.find((x) => x.repo === "fresh/no-stats-yet");
  assert.equal(e.likes, 0);
  assert.equal(e.delta_7d, 0);
  assert.equal(e.rank, null);
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
  assert.equal(liveList(entries).length, 6);
  assert.ok(entries.every((e) => e.likes === 0 && e.rank === null));
});

test("languages come from meta, deduplicated and sorted", () => {
  assert.deepEqual(languages(liveList(all().entries)), [
    "Go",
    "Rust",
    "TypeScript",
  ]);
});
