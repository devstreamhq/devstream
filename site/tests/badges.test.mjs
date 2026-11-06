import { test } from "node:test";
import assert from "node:assert/strict";
import { flatBadge } from "../src/lib/embeds.ts";
import { GET as listed } from "../src/pages/badge.svg.ts";
import { GET as scorecard } from "../src/pages/scorecard.svg.ts";

const realFetch = globalThis.fetch;
const mock = (fn) => (globalThis.fetch = fn);
const json =
  (body, ok = true) =>
  async () => ({ ok, json: async () => body });
const text = async (r) => r.text();
const checks = (svg) => {
  assert.ok(!/<script/i.test(svg), "no script");
  assert.ok(!/(href|src)\s*=\s*["']?https?:/i.test(svg), "no external refs");
  assert.ok(Buffer.byteLength(svg) < 5 * 1024, "under 5 KB");
};

test("badge.svg shows the count, no script", async () => {
  const svg = await text(listed());
  assert.match(svg, />\d+ repos?</);
  assert.ok(svg.includes(">listed<"));
  checks(svg);
});

test("scorecard.svg renders the numeric score", async () => {
  mock(json({ score: 7.34 }));
  const svg = await text(await scorecard());
  assert.ok(svg.includes(">7.3<"));
  assert.ok(svg.includes("#4c1"));
  checks(svg);
  mock(json({ score: 5 }));
  assert.ok((await text(await scorecard())).includes("#dfb317"));
  mock(json({ score: 2.1 }));
  assert.ok((await text(await scorecard())).includes("#e05d44"));
});

test("scorecard.svg says pending on throw or garbage", async () => {
  for (const f of [
    async () => {
      throw new Error("down");
    },
    json({ score: "9" }),
    json({ score: NaN }),
    json(null),
    json({ score: 8 }, false),
  ]) {
    mock(f);
    const svg = await text(await scorecard());
    assert.ok(svg.includes(">pending<"));
    assert.ok(svg.includes("#9f9f9f"));
    checks(svg);
  }
});

test("both SVGs escape their input", () => {
  const svg = flatBadge('<b>"', "<script>&", '"><x');
  assert.ok(
    !svg.includes("<script>") && !svg.includes("<b>") && !svg.includes('"><x'),
  );
  assert.ok(svg.includes("&lt;script&gt;&amp;"));
});

test.after(() => {
  globalThis.fetch = realFetch;
});
