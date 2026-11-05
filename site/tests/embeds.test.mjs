import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { loadAll, liveList } from "../src/lib/data.ts";
import { top10Svg, badge, BRAND_HEX } from "../src/lib/embeds.ts";

const fixtures = fileURLToPath(new URL("./fixtures", import.meta.url));
const live = liveList(
  loadAll({ dataDir: fixtures, statsDir: `${fixtures}/stats` }).entries,
);

function assertSafe(svg) {
  assert.ok(svg.startsWith("<svg"));
  assert.ok(!/<script/i.test(svg), "no script");
  assert.ok(!/(href|src)\s*=\s*["']?https?:/i.test(svg), "no external refs");
  assert.ok(!/url\(\s*["']?https?:/i.test(svg), "no external css refs");
  assert.ok(!/@import/i.test(svg));
  assert.ok(Buffer.byteLength(svg) < 30 * 1024, "under 30 KB");
}

test("top10.svg lists the top entries by rank with likes", () => {
  const svg = top10Svg(live);
  assertSafe(svg);
  assert.ok(svg.indexOf("fast-cli") < svg.indexOf("same-likes"));
  assert.ok(svg.includes("120"));
  assert.ok(!svg.includes("hidden-thing"));
});

test("top10.svg stops at ten rows and stays under 30 KB", () => {
  const many = Array.from({ length: 40 }, (_, i) => ({
    ...live[0],
    repo: `owner-with-a-long-name-${i}/repository-with-a-long-name-${i}`,
    likes: 100000 - i,
    rank: i + 1,
  }));
  const svg = top10Svg(many);
  assertSafe(svg);
  assert.ok(svg.includes("repository-with-a-long-name-9"));
  assert.ok(!svg.includes("repository-with-a-long-name-10"));
});

test("top10.svg escapes repo text", () => {
  const svg = top10Svg([{ ...live[0], repo: "a<b/c&d" }]);
  assert.ok(svg.includes("a&lt;b") && svg.includes("c&amp;d"));
});

test("top10.svg has an empty state", () => {
  const svg = top10Svg([]);
  assertSafe(svg);
  assert.match(svg, /filling up/i);
});

test("badge.json has the shields.io endpoint shape", () => {
  assert.deepEqual(badge(live), {
    schemaVersion: 1,
    label: "listed",
    message: "6 repos",
    color: BRAND_HEX,
  });
  assert.equal(badge(live.slice(0, 1)).message, "1 repo");
  assert.match(BRAND_HEX, /^#[0-9a-f]{6}$/i);
});
