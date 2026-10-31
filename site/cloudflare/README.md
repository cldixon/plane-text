# Deploy

The `cf` project that deploys the built site (`../dist`) on Cloudflare Workers: static assets behind a small Worker.

- `cloudflare.config.ts`: the Worker (name, compatibility date, entrypoint, assets and R2 bindings, 404 handling, custom domains). Every request runs the Worker first: it redirects the old hostname `planetext.cldixon.dev` to `plane-text.cldixon.dev` (301, same path), serves `/data/*` and `/download/*` from R2, and passes every other path to the static assets.
- `src/index.ts`: serves allowlisted files from the private R2 bucket `plane-text` at `/data/<key>`, with HEAD, byte ranges (206 + `Content-Range`), `ETag` and `Last-Modified`. DuckDB-WASM on `/shell` reads parquet through it with Range requests. To expose another file, add its key to `EXPOSED_KEYS`. It also serves releases: `/download` redirects (302, not cached) to the release `releases/latest.json` names, and `/download/plane-text-v<version>.zip` serves `releases/plane-text-v<version>.zip` as an attachment, with the same range support, so interrupted downloads resume.
- `wrangler.config.ts`: points the asset build at `../dist`. `cf build` uses Wrangler internally to package static assets; this file is that bundler's setting, not a Wrangler CLI config.

Run from `site/` with `bun run preview`, `bun run deploy` or `bun run deploy:preview`, or here with `bun run build | check | dev | deploy | deploy:preview`. `deploy:preview` uploads a Worker version without deploying it, under a preview alias named after the git branch: `https://<branch>-plane-text.cl-dixon.workers.dev`. Preview URLs are on (`previewUrls`); production stays off workers.dev (`workersDev: false`), only on the custom domain. A preview uses the same R2 binding as production. `check` generates the binding types (`cf workers types`, into `.cloudflare/types`) and type-checks the Worker.

## Local data

`cf dev` simulates the R2 bucket locally under `.wrangler/state`, so it starts empty. Seed it once with the real file (downloaded with the S3 API, e.g. boto3):

```sh
bunx cf r2 objects put processed/faa-ads/v2026-10-23/ad-final-rules.parquet --local --persist-to .wrangler/state \
  --bucket-name plane-text --content-type application/vnd.apache.parquet --file <path/to/ad-final-rules.parquet>
```

For the download, seed `releases/latest.json` (`{"file": "plane-text-v1.zip"}` is enough) and the zip it names the same way.

`--persist-to .wrangler/state` matters: without it `cf r2 --local` writes to `~/.config/cloudflare/state`, which `cf dev` does not read.

This lives apart from the Astro project because `cf` 1.0.0-beta.21 detects Astro and expects `@astrojs/cloudflare` to emit Cloudflare's Build Output, which the current adapter does not. Once it does, this can fold back into `site/`.
