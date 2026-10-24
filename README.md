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
| `ntsb-lookups` | NTSB database's embedded data dictionary and code tables | column definitions and code meanings, four tables | in R2 |
| `ntsb-findings` | NTSB findings, event sequences, occurrences and sequence of events | one row per coded item, four tables by coding era | in R2 |

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
uv run python -m plane_text.ntsb                # NTSB narratives -> data/ntsb-narratives/v<today>/narratives.parquet
uv run python -m plane_text.ntsb_lookups        # NTSB code tables -> data/ntsb-lookups/v<today>/<table>.parquet
uv run python -m plane_text.ntsb_findings       # NTSB coded findings -> data/ntsb-findings/v<today>/<table>.parquet
```

Each parquet file carries its provenance (source, source URL, snapshot, retrieval time) as `plane_text.*` key-value metadata.

### Raw and processed layers

The FAA AD and SDR pipelines write two layers, locally under `data/` and under the same keys in R2:

- `raw/<dataset>/v<snapshot>/<file>.parquet`: the source as delivered, converted to parquet. The source's own field names, every value as text, nothing cleaned, decoded or dropped, plus provenance (`source_file`/`source_row` or `page_offset`/`page_row`). Everything downstream can be rebuilt from raw alone.
- `processed/<dataset>/v<snapshot>/<file>.parquet`: the user-facing tables, built from raw only: cleaned, typed, one row per real-world entity, with codes decoded to labels. Provenance adds `built_from` (the raw key) and `built_at`.

Source downloads are cached under `data/cache/<dataset>/`, so re-runs only re-parse. `--snapshot` names the version (default today); `--step raw` or `--step processed` builds one layer (default both, raw first); `--upload` uploads what was built. Build the lookups before the SDRs: processed SDRs take their labels from the processed lookups.

```sh
uv run python -m plane_text.faa_sdrs_lookups    # raw/faa-sdrs-lookups/v<today>/<Workbook>.parquet, processed/.../<table>.parquet
uv run python -m plane_text.faa_sdrs            # raw|processed/faa-sdrs/v<today>/service-difficulty-reports.parquet
uv run python -m plane_text.faa_ads             # raw|processed/faa-ads/v<today>/ad-final-rules.parquet
uv run python -m plane_text.faa_sdrs --step processed --snapshot 2026-10-23   # rebuild processed from raw
uv run python -m plane_text.faa_sdrs --years 2024 2025 --upload
```

| Dataset | Raw | Processed |
| --- | --- | --- |
| `faa-ads` | every DRS record as read (duplicates kept), API field names (`documentGuid`, `drs:title`, ...), text and lists of text | one row per DRS document (`guid`): placeholders dropped, whitespace collapsed, dates typed |
| `faa-sdrs` | every row of every yearly CSV, the FAA's 76 column names, text (empty fields null) | one row per report (`operator_control_number`): snake_case, trimmed, dates and measures typed, codes uppercased with labels beside them |
| `faa-sdrs-lookups` | each workbook in the Look-Up Tables zip with its own headers, and the JASC PDF's text lines (`JASC_Code`) | one cleaned table per code set, plus `jasc` (chapter, chapter title, short title, reporting instructions) |

In processed SDRs, every code column `x_code` is followed by its label `x` (submitter type, SDR type, nature of condition a–c, precautionary procedure a–d, stage of operation, how discovered, part and component time-since codes), `jasc_code` by `jasc_chapter`, `jasc_chapter_title` and `jasc_title`, and `part_location` (free text) by `part_location_zone_code` and `part_location_zone` where it names a zone (`ZONE 200`, `ZN 200 FUS TOP` and sub-zones like `ZONE 221` map to major zone 200). A code with no entry in the lookup keeps its code and gets a null label. Hours, cycles, crack length, number of cracks and corrosion level are numeric; a value that does not parse becomes null, the count of such values is recorded in the `plane_text.unparsed` metadata, and the original text stays in raw. Station, stringer, butt line and water line columns are location designators (`727B`, `RIB 10`, `Y1292`), so they stay text.

Uploading needs R2's S3-compatible credentials in the environment: `S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The bucket defaults to `plane-text`; override it with `PLANETEXT_BUCKET`. The FAA ADs raw step also needs `DRS_API_KEY`. The NTSB pipeline needs `mdb-export` from [mdbtools](https://github.com/mdbtools/mdbtools) (`apt-get install mdbtools`) to read the NTSB's Access files.

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

`/explore` is an in-browser SQL viewer: DuckDB-WASM (loaded from jsDelivr) queries the airworthiness directives parquet, which a small Worker serves from the private R2 bucket at `/data/*` with HTTP Range requests. See `site/cloudflare/README.md`, including how to seed the local R2 bucket for `bun run preview`.

Design tokens (colors, type, space) live in `site/src/design/tokens.css`; see `site/src/design/README.md`.
