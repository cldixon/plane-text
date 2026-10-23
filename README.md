# plane-text

An open-source corpus of aviation maintenance text: regulations, airworthiness directives, accident and incident reports, and more, gathered from public sources and published as parquet datasets.

## Datasets

| Dataset | Source | Grain | Status |
| --- | --- | --- | --- |
| `ecfr-title14` | 14 CFR (Aeronautics and Space), eCFR versioner API | one row per section or appendix | in R2 |
| `faa-ads` | FAA AD final rules (`ADFRAWD`), DRS API via [`faa-drs`](https://pypi.org/project/faa-drs/) | one row per DRS document (corrections are separate rows) | in R2 |
| `faa-sdrs` | FAA Service Difficulty Reports, yearly CSV downloads, 1995 onward | one row per report | in R2 |
| `faa-sdrs-lookups` | FAA SDRS Look-Up Tables and the 2008 JASC code table | one small table per SDR code set | in R2 |
| `ntsb-narratives` | NTSB aviation accident database (`avall.zip`, `Pre2008.zip`), 1982 onward | one row per aircraft per event | in R2 |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

## Running a pipeline

```sh
uv run python -m plane_text.ecfr                # latest issue -> data/ecfr-title14/v<date>/sections.parquet
uv run python -m plane_text.ecfr --limit 5      # first five parts only
uv run python -m plane_text.ecfr --upload       # also upload to R2
uv run python -m plane_text.faa_ads             # all AD final rules -> data/faa-ads/v<today>/ad-final-rules.parquet
uv run python -m plane_text.faa_sdrs            # all SDR years -> data/faa-sdrs/v<today>/service-difficulty-reports.parquet
uv run python -m plane_text.faa_sdrs_lookups    # SDR code tables -> data/faa-sdrs-lookups/v<today>/<table>.parquet
uv run python -m plane_text.ntsb                # NTSB narratives -> data/ntsb-narratives/v<today>/narratives.parquet
```

Raw source files are cached under `data/raw/`, so re-runs only re-parse. Each parquet file carries its provenance (source, source URL, issue date, retrieval time) as `plane_text.*` key-value metadata.

Uploading needs R2's S3-compatible credentials in the environment: `S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The bucket defaults to `plane-text`; override it with `PLANETEXT_BUCKET`. The FAA ADs pipeline also needs `DRS_API_KEY`. The NTSB pipeline needs `mdb-export` from [mdbtools](https://github.com/mdbtools/mdbtools) (`apt-get install mdbtools`) to read the NTSB's Access files.

## Site

The landing page lives in `site/`: Astro with Svelte and TypeScript, served as static assets on Cloudflare Workers at `planetext.cldixon.dev`. Requires [Bun](https://bun.sh); Astro runs on the Bun runtime.

```sh
cd site
bun install && bun install --cwd cloudflare
bun run dev       # Astro dev server
bun run build     # type-check, then build to site/dist
bun run preview   # build, then serve the built site locally with cf dev
bun run deploy    # build, then deploy with cf
```

Deployment uses Cloudflare's [`cf`](https://github.com/cloudflare/cf) CLI (beta), configured in `site/cloudflare/cloudflare.config.ts`. It is a separate project inside `site/` because `cf` does not yet build Astro projects directly: Astro builds `site/dist`, and `cf` packages that directory as static assets. `cf` itself runs on Node 22.18 or later, since it cannot load `cloudflare.config.ts` on Bun yet. Deploying needs `CLOUDFLARE_API_TOKEN`.

Design tokens (colors, type, space) live in `site/src/design/tokens.css`; see `site/src/design/README.md`.
