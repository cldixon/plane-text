import { bindings, defineConfig } from "cf/config";

export default defineConfig({
	worker: {
		name: "plane-text",
		compatibilityDate: "2026-10-01",
		// Serves /data/* from R2; every other path is a static asset.
		entrypoint: "./src/index.ts",
		assets: {
			notFoundHandling: "404-page",
			runWorkerFirst: ["/data/*"],
		},
		env: {
			DATA: bindings.r2({ name: "plane-text" }),
		},
		domains: ["planetext.cldixon.dev"],
		// Production is served only on the custom domain; uploaded versions get
		// preview URLs on workers.dev (cf workers versions create --preview-alias).
		workersDev: false,
		previewUrls: true,
	},
});
