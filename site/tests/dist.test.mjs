// Builds the site from the fixture data into a temp dir, then checks the output.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const site = fileURLToPath(new URL("..", import.meta.url));
const fixtures = join(site, "tests/fixtures");
const out = mkdtempSync(join(tmpdir(), "devstream-dist-"));

const walk = (dir) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
    d.isDirectory() ? walk(join(dir, d.name)) : [join(dir, d.name)],
  );
const read = (p) => readFileSync(join(out, p), "utf8");
let files;

before(() => {
  execFileSync(join(site, "node_modules/.bin/astro"), ["build"], {
    cwd: site,
    env: {
      ...process.env,
      DATA_DIR: fixtures,
      STATS_DIR: join(fixtures, "stats"),
      OUT_DIR: out,
    },
    stdio: "pipe",
  });
  files = walk(out).filter((f) => /\.(html|svg|json|js|css|txt)$/.test(f));
});

test("hidden entries are never present in the build", () => {
  for (const f of files) {
    const s = readFileSync(f, "utf8");
    assert.ok(!s.includes("HIDDENMARKER") && !s.includes("hidden-thing"), f);
  }
});

test("archived and superseded entries appear only on /archived", () => {
  for (const f of files) {
    const s = readFileSync(f, "utf8");
    const onArchive = f === join(out, "archived/index.html");
    for (const m of ["ARCHIVEDMARKER", "SUPERSEDEDMARKER"])
      assert.equal(s.includes(m), onArchive, `${m} in ${f}`);
  }
});

test("an entry with no stats still renders", () => {
  assert.ok(read("index.html").includes("no-stats-yet"));
  assert.ok(read("category/mobile/index.html").includes("no-stats-yet"));
});

test("every category gets a page", () => {
  const cats = JSON.parse(
    readFileSync(join(fixtures, "categories.json"), "utf8"),
  );
  for (const c of cats)
    assert.ok(existsSync(join(out, `category/${c.slug}/index.html`)), c.slug);
});

test("the leaderboard is a server-rendered table with Top active", () => {
  const html = read("index.html");
  assert.match(html, /<table/);
  assert.match(html, /role="tab"[^>]*aria-selected="true"[^>]*>\s*Top/);
  assert.ok(html.indexOf("fast-cli") < html.indexOf("same-likes"));
});

test("no inline script, style or handler survives the CSP", () => {
  for (const f of files.filter((f) => f.endsWith(".html"))) {
    const html = readFileSync(f, "utf8");
    for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) {
      assert.ok(
        /\bsrc=/.test(tag) || /type="application\/json"/.test(tag),
        `${f}: ${tag}`,
      );
    }
    assert.ok(!/<style\b/i.test(html), `${f}: <style>`);
    assert.ok(!/\sstyle=/i.test(html), `${f}: style attribute`);
    assert.ok(!/\son[a-z]+=/i.test(html), `${f}: inline handler`);
    assert.ok(!/name="generator"/i.test(html), `${f}: generator meta`);
  }
});

test("only same-origin and avatar URLs are requested", () => {
  for (const f of files.filter((f) => f.endsWith(".html"))) {
    const html = readFileSync(f, "utf8");
    for (const [, url] of html.matchAll(/\s(?:src|srcset)="(https?:[^"]+)"/g)) {
      assert.ok(
        url.startsWith("https://avatars.githubusercontent.com/"),
        `${f}: ${url}`,
      );
    }
    for (const tag of html.match(/<link\b[^>]*>/g) ?? []) {
      if (!tag.includes('rel="canonical"'))
        assert.ok(!/href="https?:/.test(tag), `${f}: ${tag}`);
    }
  }
});

test("top10.svg and badge.json are built", () => {
  const svg = read("top10.svg");
  assert.ok(
    svg.startsWith("<svg") && !/<script/i.test(svg) && svg.length < 30 * 1024,
  );
  assert.equal(JSON.parse(read("badge.json")).message, "7 repos");
});

test("hostile stats are neutralised in the HTML and the SVG", () => {
  for (const p of ["index.html", "category/security/index.html", "top10.svg"]) {
    const s = read(p);
    if (p !== "top10.svg") assert.ok(s.includes("hostile-stats"), p);
    for (const bad of [
      "javascript:",
      "onload=x",
      "9<x>",
      "<script>",
      "evil.example.com",
      "12<x>",
      "1e3<x>",
    ]) {
      assert.ok(!s.includes(bad), `${p}: ${bad}`);
    }
  }
});

test("the Trending tab marks exactly the rows trending() allows", async () => {
  const { loadAll, trending } = await import("../src/lib/data.ts");
  const want = trending(
    loadAll({ dataDir: fixtures, statsDir: join(fixtures, "stats") }).entries,
  ).map((e) => e.repo.toLowerCase());
  for (const page of ["index.html", "category/cli-terminal/index.html"]) {
    const rows = read(page).match(/<tr\b[^>]*data-q=[^>]*>/g) ?? [];
    const marked = rows
      .filter((r) => /\bdata-trend\b/.test(r))
      .map((r) => r.match(/data-q="([^ "]+)/)[1]);
    assert.ok(rows.length > 0, page);
    // zz-tie/same-likes climbs by 3, under TRENDING_MIN_DELTA, so the hero would skip it.
    assert.ok(!marked.includes("zz-tie/same-likes"), page);
    assert.deepEqual(
      marked.sort(),
      want.filter((r) => rows.some((x) => x.includes(`data-q="${r} `))).sort(),
      page,
    );
  }
});
