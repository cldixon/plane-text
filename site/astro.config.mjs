// @ts-check
import { defineConfig } from "astro/config";
import svelte from "@astrojs/svelte";

export default defineConfig({
  site: "https://planetext.cldixon.dev",
  integrations: [svelte()],
});
