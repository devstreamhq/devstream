// Starts DevStream's timed GitHub workflows. GitHub's own scheduler skips runs
// under load (Likes once went seven hours without one); Cloudflare's doesn't.

const API =
  "https://api.github.com/repos/devstreamhq/devstream/actions/workflows";

// One cron trigger, every 15 minutes; the tick's time picks what else runs.
export function workflowsFor(when) {
  const m = when.getUTCMinutes();
  const h = when.getUTCHours();
  const out = ["sweep.yml"];
  if (m === 15) out.push("likes.yml");
  if (m === 30) out.push("on-merge.yml");
  // Monday; kept off :30 so it never shares the list-writes queue with on-merge.
  if (when.getUTCDay() === 1 && h === 6 && m === 45) out.push("rot.yml");
  if (h === 9 && m === 45) out.push("stars.yml");
  return out;
}

export async function dispatch(workflow, token, fetchFn = fetch) {
  const res = await fetchFn(`${API}/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "devstream-scheduler",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (res.status !== 204) {
    throw new Error(`${workflow}: GitHub answered ${res.status}`);
  }
}

export async function run(when, token, fetchFn = fetch) {
  // Every dispatch is tried; one failure mustn't stop the rest.
  const results = await Promise.allSettled(
    workflowsFor(when).map((wf) => dispatch(wf, token, fetchFn)),
  );
  const failed = results.filter((r) => r.status === "rejected");
  if (failed.length) {
    // Thrown so the Cron Trigger shows as failed in the Cloudflare dashboard.
    throw new Error(failed.map((r) => r.reason.message).join("; "));
  }
}

export default {
  async scheduled(controller, env) {
    // scheduledTime is the planned slot, not when the tick actually ran; drop any seconds.
    const when = new Date(Math.floor(controller.scheduledTime / 60000) * 60000);
    await run(when, env.GITHUB_TOKEN);
  },
};
