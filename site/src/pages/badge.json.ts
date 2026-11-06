import { loadAll, liveList } from "../lib/data.ts";
import { badge } from "../lib/embeds.ts";

export function GET() {
  return new Response(JSON.stringify(badge(liveList(loadAll().entries))), {
    headers: { "Content-Type": "application/json" },
  });
}
