// @ts-check
import { defineConfig } from "astro/config";
import svelte from "@astrojs/svelte";

export default defineConfig({
  site: "https://plane-text.cldixon.dev",
  integrations: [svelte()],
  // The explorer became the Shell tab.
  redirects: { "/explore": "/shell" },
});
