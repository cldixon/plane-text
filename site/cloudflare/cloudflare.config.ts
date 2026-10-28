import { bindings, defineConfig } from "cf/config";

export default defineConfig({
	worker: {
		name: "plane-text",
		compatibilityDate: "2026-10-01",
		// Every request reaches the Worker first: it redirects the old hostname,
		// serves /data/* from R2, and hands every other path to the static assets.
		entrypoint: "./src/index.ts",
		assets: {
			notFoundHandling: "404-page",
			runWorkerFirst: true,
		},
		env: {
			ASSETS: bindings.assets(),
			DATA: bindings.r2({ name: "plane-text" }),
		},
		// The site's name, then the old one, kept only to redirect to it.
		domains: ["plane-text.cldixon.dev", "planetext.cldixon.dev"],
		// Production is served only on the custom domain; uploaded versions get
		// preview URLs on workers.dev (cf workers versions create --preview-alias).
		workersDev: false,
		previewUrls: true,
	},
});
