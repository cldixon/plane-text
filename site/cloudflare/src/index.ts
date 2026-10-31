/**
 * The site's Worker. Every request reaches it first (`runWorkerFirst` in
 * cloudflare.config.ts):
 *
 * - the old hostname, planetext.cldixon.dev, redirects (301) to the same path
 *   on plane-text.cldixon.dev;
 * - /data/<key> serves an allowlisted dataset file from the private R2 bucket.
 *   DuckDB-WASM reads parquet with HTTP Range requests, so this supports HEAD,
 *   single byte ranges (206 + Content-Range) and ETag / Last-Modified validators;
 * - /download redirects (302) to the latest release, as named by
 *   releases/latest.json, and /download/plane-text-v<version>.zip serves that release
 *   from R2 as an attachment, with the same range support (so downloads resume);
 * - everything else is a static asset.
 *
 * `Env` (the `ASSETS` and R2 `DATA` bindings) is generated from cloudflare.config.ts
 * by `cf workers types` into .cloudflare/types/.
 */

const HOST = "plane-text.cldixon.dev";
const OLD_HOST = "planetext.cldixon.dev";

/** R2 object keys exposed to the browser. Nothing else in the bucket is reachable. */
const EXPOSED_KEYS = new Set<string>(["processed/faa-ads/v2026-10-23/ad-final-rules.parquet"]);

const PREFIX = "/data/";

const DOWNLOAD = "/download";
/** Written by `python -m plane_text.release` after the release's zip. */
const LATEST_RELEASE = "releases/latest.json";
/** plane-text-v<major>.<minor>.zip; the first release, v1, predates minor versions. */
const RELEASE_FILE = /^plane-text-v\d+(\.\d+)?\.zip$/;

type ByteRange = { offset: number; length: number };

/**
 * Parses a single-range `Range: bytes=…` header against the object size.
 * Returns null when there is no usable header (serve the whole object),
 * or "unsatisfiable" for a well-formed range outside the object.
 */
function parseRange(header: string | null, size: number): ByteRange | null | "unsatisfiable" {
	if (!header) return null;
	const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
	if (!match) return null; // Multi-range or malformed: ignore, as RFC 9110 allows.
	const [, startText = "", endText = ""] = match;
	if (startText === "" && endText === "") return null;

	if (startText === "") {
		// Suffix range: the last N bytes.
		const suffix = Number(endText);
		if (suffix === 0) return "unsatisfiable";
		const length = Math.min(suffix, size);
		return { offset: size - length, length };
	}

	const start = Number(startText);
	if (start >= size) return "unsatisfiable";
	const end = endText === "" ? size - 1 : Math.min(Number(endText), size - 1);
	if (end < start) return null;
	return { offset: start, length: end - start + 1 };
}

function baseHeaders(object: R2Object, contentType: string): Headers {
	const headers = new Headers();
	object.writeHttpMetadata(headers);
	if (!headers.has("content-type")) headers.set("content-type", contentType);
	headers.set("etag", object.httpEtag);
	headers.set("last-modified", object.uploaded.toUTCString());
	headers.set("accept-ranges", "bytes");
	headers.set("cache-control", "public, max-age=3600");
	return headers;
}

type Served = { contentType: string; headers?: Record<string, string> };

/** Serves one R2 object with HEAD, a single byte range and ETag / If-Range validators. */
async function serveObject(request: Request, env: Env, key: string, served: Served): Promise<Response> {
	if (request.method !== "GET" && request.method !== "HEAD") {
		return new Response("Method not allowed\n", { status: 405, headers: { allow: "GET, HEAD" } });
	}

	const head = await env.DATA.head(key);
	if (!head) return new Response("Not found\n", { status: 404 });

	const headers = baseHeaders(head, served.contentType);
	for (const [name, value] of Object.entries(served.headers ?? {})) headers.set(name, value);
	const ifNoneMatch = request.headers.get("if-none-match");
	if (ifNoneMatch && ifNoneMatch === head.httpEtag) {
		return new Response(null, { status: 304, headers });
	}

	// Ignore Range when If-Range names a different version of the object.
	const ifRange = request.headers.get("if-range");
	const rangeHeader = ifRange && ifRange !== head.httpEtag ? null : request.headers.get("range");
	const range = parseRange(rangeHeader, head.size);

	if (range === "unsatisfiable") {
		headers.set("content-range", `bytes */${head.size}`);
		return new Response(null, { status: 416, headers });
	}

	const length = range ? range.length : head.size;
	headers.set("content-length", String(length));
	if (range) {
		headers.set("content-range", `bytes ${range.offset}-${range.offset + range.length - 1}/${head.size}`);
	}
	const status = range ? 206 : 200;

	if (request.method === "HEAD") return new Response(null, { status, headers });

	const object = await env.DATA.get(key, range ? { range } : undefined);
	if (!object) return new Response("Not found\n", { status: 404 });
	return new Response(object.body, { status, headers });
}

/** /download: a redirect to the latest release; /download/<file>: that release's zip. */
async function download(request: Request, env: Env, url: URL): Promise<Response> {
	if (url.pathname === DOWNLOAD || url.pathname === `${DOWNLOAD}/`) {
		const latest = await env.DATA.get(LATEST_RELEASE);
		const file = latest ? ((await latest.json()) as { file?: string }).file : undefined;
		if (!file || !RELEASE_FILE.test(file)) {
			return new Response("No release has been published yet.\n", { status: 503, headers: { "retry-after": "3600" } });
		}
		return new Response(null, {
			status: 302,
			headers: { location: `${DOWNLOAD}/${file}`, "cache-control": "no-store" },
		});
	}

	const file = url.pathname.slice(DOWNLOAD.length + 1);
	if (!RELEASE_FILE.test(file)) return new Response("Not found\n", { status: 404 });
	return serveObject(request, env, `releases/${file}`, {
		contentType: "application/zip",
		// A release never changes once published; its number names a new one.
		headers: { "content-disposition": `attachment; filename="${file}"`, "cache-control": "public, max-age=86400" },
	});
}

export default {
	async fetch(request, env): Promise<Response> {
		const url = new URL(request.url);
		if (url.hostname === OLD_HOST) {
			url.hostname = HOST;
			return Response.redirect(url.toString(), 301);
		}
		if (url.pathname === DOWNLOAD || url.pathname.startsWith(`${DOWNLOAD}/`)) return download(request, env, url);
		if (!url.pathname.startsWith(PREFIX)) return env.ASSETS.fetch(request);

		const key = decodeURIComponent(url.pathname.slice(PREFIX.length));
		if (!EXPOSED_KEYS.has(key)) {
			return new Response("Not found\n", { status: 404 });
		}
		return serveObject(request, env, key, { contentType: "application/vnd.apache.parquet" });
	},
} satisfies ExportedHandler<Env>;
