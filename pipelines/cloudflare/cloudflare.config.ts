import { bindings, defineConfig, defineContainer, exports } from "cf/config";

// The plane_text pipelines image, built from the repository root.
const pipelines = defineContainer({
	name: "plane-text-pipelines",
	schedulingPolicy: "durable-object",
	images: {
		pipelines: { dockerfile: "./Dockerfile", buildContext: "../.." },
	},
	observability: { enabled: true, logs: { enabled: true } },
});

export default defineConfig({
	worker: {
		name: "plane-text-pipelines",
		compatibilityDate: "2026-10-01",
		// POST /runs/<pipeline> starts a run in a Container; GET reads its status.
		// The Worker reaches its own PipelineRun objects through `ctx.exports`, not a binding.
		entrypoint: "./src/index.ts",
		observability: { enabled: true },
		exports: {
			PipelineRun: exports.durableObject({ storage: "sqlite", container: pipelines }),
		},
		env: {
			// Bearer token for the run endpoints.
			RUN_TOKEN: bindings.secret(),
			// Passed to the container: DRS access and R2 (S3 API) credentials.
			DRS_API_KEY: bindings.secret(),
			S3_API_ENDPOINT: bindings.secret(),
			AWS_ACCESS_KEY_ID: bindings.secret(),
			AWS_SECRET_ACCESS_KEY: bindings.secret(),
		},
	},
	containers: [pipelines],
});
