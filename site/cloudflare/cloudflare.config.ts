import { defineConfig } from "cf/config";

export default defineConfig({
	worker: {
		name: "plane-text",
		compatibilityDate: "2026-10-01",
		assets: {
			notFoundHandling: "404-page",
		},
		domains: ["planetext.cldixon.dev"],
	},
});
