import { flatBadge } from "../lib/embeds.ts";

const URL_ =
  "https://api.scorecard.dev/projects/github.com/devstreamhq/devstream";

export async function GET() {
  let msg = "pending";
  let color = "#9f9f9f";
  try {
    const res = await fetch(URL_, { signal: AbortSignal.timeout(10_000) });
    const score = (await res.json())?.score;
    if (res.ok && typeof score === "number" && Number.isFinite(score)) {
      msg = score.toFixed(1);
      color = score >= 7 ? "#4c1" : score >= 4 ? "#dfb317" : "#e05d44";
    }
  } catch {
    // Never fail the build over a badge.
  }
  return new Response(flatBadge("openssf scorecard", msg, color), {
    headers: { "Content-Type": "image/svg+xml; charset=utf-8" },
  });
}
