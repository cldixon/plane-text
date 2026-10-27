# Deploy

The `cf` project that deploys the built site (`../dist`) on Cloudflare Workers: static assets, plus a small Worker for `/data/*`.

- `cloudflare.config.ts`: the Worker (name, compatibility date, entrypoint, R2 binding, 404 handling, custom domain). Only `/data/*` runs the Worker first; every other path is a static asset.
- `src/index.ts`: serves allowlisted files from the private R2 bucket `plane-text` at `/data/<key>`, with HEAD, byte ranges (206 + `Content-Range`), `ETag` and `Last-Modified`. DuckDB-WASM on `/explore` reads parquet through it with Range requests. To expose another file, add its key to `EXPOSED_KEYS`.
- `wrangler.config.ts`: points the asset build at `../dist`. `cf build` uses Wrangler internally to package static assets; this file is that bundler's setting, not a Wrangler CLI config.

Run from `site/` with `bun run preview` or `bun run deploy`, or here with `bun run build | check | dev | deploy`. `check` generates the binding types (`cf workers types`, into `.cloudflare/types`) and type-checks the Worker.

## Local data

`cf dev` simulates the R2 bucket locally under `.wrangler/state`, so it starts empty. Seed it once with the real file (downloaded with the S3 API, e.g. boto3):

```sh
bunx cf r2 objects put processed/faa-ads/v2026-10-23/ad-final-rules.parquet --local --persist-to .wrangler/state \
  --bucket-name plane-text --content-type application/vnd.apache.parquet --file <path/to/ad-final-rules.parquet>
```

`--persist-to .wrangler/state` matters: without it `cf r2 --local` writes to `~/.config/cloudflare/state`, which `cf dev` does not read.

This lives apart from the Astro project because `cf` 1.0.0-beta.21 detects Astro and expects `@astrojs/cloudflare` to emit Cloudflare's Build Output, which the current adapter does not. Once it does, this can fold back into `site/`.
