import { test } from "node:test";
import assert from "node:assert/strict";
import { centralStamp, lastCount } from "../src/lib/format.ts";

test("centralStamp shows CST in winter", () => {
  assert.equal(
    centralStamp(new Date("2026-11-06T03:48:00Z")),
    "Nov 5, 9:48 PM CST",
  );
});

test("centralStamp shows CDT in summer", () => {
  assert.equal(
    centralStamp(new Date("2026-07-01T17:00:00Z")),
    "Jul 1, 12:00 PM CDT",
  );
});

test("lastCount uses the last Likes run, else the build time", () => {
  const now = new Date("2026-11-07T12:00:00Z");
  assert.equal(
    lastCount("2026-11-07T09:17:42Z", now).toISOString(),
    "2026-11-07T09:17:42.000Z",
  );
  for (const bad of [undefined, "", "null", "yesterday", "2026-11-07"]) {
    assert.equal(lastCount(bad, now), now);
  }
});
