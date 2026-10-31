// @ts-check
import { defineConfig } from "astro/config";
import svelte from "@astrojs/svelte";

export default defineConfig({
  site: "https://plane-text.cldixon.dev",
  integrations: [svelte()],
  // Build each page as one file (shell.html), so Cloudflare serves /shell
  // as is, rather than redirecting it to /shell/.
  build: { format: "file" },
  // The explorer became the Shell tab; the Files tab gave way to the download.
  redirects: { "/explore": "/shell", "/files": "/" },
});
