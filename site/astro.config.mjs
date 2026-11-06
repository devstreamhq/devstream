import { defineConfig } from "astro/config";

// The CSP in public/_headers allows no inline script or style, so nothing may be inlined.
export default defineConfig({
  site: "https://devstream.jamesconway.dev",
  outDir: process.env.OUT_DIR || "./dist",
  build: { inlineStylesheets: "never" },
  vite: { build: { assetsInlineLimit: 0 } },
  devToolbar: { enabled: false },
});
