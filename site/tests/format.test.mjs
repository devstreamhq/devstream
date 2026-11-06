import { test } from "node:test";
import assert from "node:assert/strict";
import { centralStamp } from "../src/lib/format.ts";

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
