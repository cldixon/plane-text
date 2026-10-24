/**
 * Serves allowlisted dataset files from the private R2 bucket at /data/<key>.
 *
 * Only /data/* reaches this Worker (see `runWorkerFirst` in cloudflare.config.ts);
 * everything else is a static asset. DuckDB-WASM reads parquet with HTTP Range
 * requests, so this supports HEAD, single byte ranges (206 + Content-Range) and
 * ETag / Last-Modified validators.
 *
 * `Env` (the R2 binding `DATA`) is generated from cloudflare.config.ts by
 * `cf workers types` into .cloudflare/types/.
 */

/** R2 object keys exposed to the browser. Nothing else in the bucket is reachable. */
const EXPOSED_KEYS = new Set<string>(["faa-ads/v2026-10-23/ad-final-rules.parquet"]);

const PREFIX = "/data/";

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

function baseHeaders(object: R2Object): Headers {
	const headers = new Headers();
	object.writeHttpMetadata(headers);
	if (!headers.has("content-type")) headers.set("content-type", "application/vnd.apache.parquet");
	headers.set("etag", object.httpEtag);
	headers.set("last-modified", object.uploaded.toUTCString());
	headers.set("accept-ranges", "bytes");
	headers.set("cache-control", "public, max-age=3600");
	return headers;
}

export default {
	async fetch(request, env): Promise<Response> {
		const url = new URL(request.url);
		const key = decodeURIComponent(url.pathname.slice(PREFIX.length));

		if (!url.pathname.startsWith(PREFIX) || !EXPOSED_KEYS.has(key)) {
			return new Response("Not found\n", { status: 404 });
		}
		if (request.method !== "GET" && request.method !== "HEAD") {
			return new Response("Method not allowed\n", { status: 405, headers: { allow: "GET, HEAD" } });
		}

		const head = await env.DATA.head(key);
		if (!head) return new Response("Not found\n", { status: 404 });

		const headers = baseHeaders(head);
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
	},
} satisfies ExportedHandler<Env>;
