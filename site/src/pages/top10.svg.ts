import { loadAll, liveList } from "../lib/data.ts";
import { top10Svg } from "../lib/embeds.ts";

export function GET() {
  return new Response(top10Svg(liveList(loadAll().entries)), {
    headers: { "Content-Type": "image/svg+xml; charset=utf-8" },
  });
}
