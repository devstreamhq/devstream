import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import worker, { dispatch, run, workflowsFor } from "../../scheduler/index.js";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const at = (iso) => new Date(iso);

function fakeFetch(statuses = {}) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const wf = url.split("/").at(-2);
    return { status: statuses[wf] ?? 204 };
  };
  return { fn, calls };
}

test("every tick starts the sweep, and only the sweep off the hourly slots", () => {
  assert.deepEqual(workflowsFor(at("2026-11-07T10:00:00Z")), ["sweep.yml"]);
  assert.deepEqual(workflowsFor(at("2026-11-07T10:45:00Z")), ["sweep.yml"]);
});

test("likes at :15 and on-merge at :30, every hour", () => {
  assert.deepEqual(workflowsFor(at("2026-11-07T03:15:00Z")), [
    "sweep.yml",
    "likes.yml",
  ]);
  assert.deepEqual(workflowsFor(at("2026-11-07T03:30:00Z")), [
    "sweep.yml",
    "on-merge.yml",
  ]);
});

test("the health check runs Mondays at 06:45 only", () => {
  // 2026-11-09 is a Monday.
  assert.deepEqual(workflowsFor(at("2026-11-09T06:45:00Z")), [
    "sweep.yml",
    "rot.yml",
  ]);
  assert.deepEqual(workflowsFor(at("2026-11-10T06:45:00Z")), ["sweep.yml"]);
  assert.deepEqual(workflowsFor(at("2026-11-09T07:45:00Z")), ["sweep.yml"]);
});

test("stars runs daily at 09:45", () => {
  assert.deepEqual(workflowsFor(at("2026-11-07T09:45:00Z")), [
    "sweep.yml",
    "stars.yml",
  ]);
});

test("a dispatch posts to main with the token", async () => {
  const { fn, calls } = fakeFetch();
  await dispatch("likes.yml", "tok", fn);
  assert.equal(
    calls[0].url,
    "https://api.github.com/repos/devstreamhq/devstream/actions/workflows/likes.yml/dispatches",
  );
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
  assert.ok(calls[0].init.headers["User-Agent"]);
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main" });
});

test("one failed dispatch still lets the others go, then fails the tick", async () => {
  const { fn, calls } = fakeFetch({ "sweep.yml": 500 });
  await assert.rejects(
    run(at("2026-11-07T03:15:00Z"), "tok", fn),
    /sweep\.yml: GitHub answered 500/,
  );
  assert.equal(calls.length, 2);
});

test("a late tick still counts as the slot it was scheduled for", async () => {
  const { fn, calls } = fakeFetch();
  const real = globalThis.fetch;
  globalThis.fetch = fn;
  try {
    const slot = Date.parse("2026-11-07T03:15:00Z");
    await worker.scheduled(
      { scheduledTime: slot + 20_000 },
      { GITHUB_TOKEN: "tok" },
    );
  } finally {
    globalThis.fetch = real;
  }
  assert.deepEqual(
    calls.map((c) => c.url.split("/").at(-2)),
    ["sweep.yml", "likes.yml"],
  );
});

test("the worker's cron and the workflows' own schedules don't double up", () => {
  assert.match(
    read("../../scheduler/wrangler.toml"),
    /crons = \["\*\/15 \* \* \* \*"\]/,
  );
  // The Worker owns these; a second, GitHub-scheduled run would queue behind it.
  for (const wf of ["likes", "on-merge", "rot"]) {
    assert.ok(
      !read(`../../.github/workflows/${wf}.yml`).includes("schedule:"),
      `${wf}.yml still has a schedule`,
    );
  }
});
