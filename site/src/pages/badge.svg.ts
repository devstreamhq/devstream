import { loadAll, liveList } from "../lib/data.ts";
import { badge, flatBadge } from "../lib/embeds.ts";

export function GET() {
  const b = badge(liveList(loadAll().entries));
  return new Response(flatBadge(b.label, b.message, b.color), {
    headers: { "Content-Type": "image/svg+xml; charset=utf-8" },
  });
}
