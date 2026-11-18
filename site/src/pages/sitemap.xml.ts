import { loadAll } from "../lib/data.ts";

// Without this file Cloudflare Pages answers /sitemap.xml with the home page.
export function GET({ site }: { site: URL }) {
  const paths = [
    "/",
    "/how-it-works/",
    "/archived/",
    ...loadAll().categories.map((c) => `/category/${c.slug}/`),
  ];
  const urls = paths
    .map((p) => `  <url><loc>${new URL(p, site)}</loc></url>`)
    .join("\n");
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
    { headers: { "Content-Type": "application/xml" } },
  );
}
