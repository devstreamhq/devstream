import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("the CSP allows images only from self and GitHub avatars", () => {
  const csp = read("../public/_headers").match(
    /Content-Security-Policy: (.*)/,
  )[1];
  assert.match(
    csp,
    /img-src 'self' https:\/\/avatars\.githubusercontent\.com;/,
  );
  assert.ok(!csp.includes("data:"));
});

test("deploy uses the exact wrangler version pinned in the lockfile", () => {
  const pinned = JSON.parse(read("../package.json")).devDependencies.wrangler;
  assert.match(pinned, /^4\.\d+\.\d+$/);
  const lock = JSON.parse(read("../package-lock.json"));
  assert.equal(lock.packages["node_modules/wrangler"].version, pinned);
  const wf = read("../../.github/workflows/deploy.yml");
  const versions = [...wf.matchAll(/WRANGLER_VERSION: "([^"]+)"/g)].map(
    (m) => m[1],
  );
  assert.deepEqual(versions, [pinned]);
});
