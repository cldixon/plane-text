# plane-text

An open-source corpus of aviation maintenance text: regulations, airworthiness directives, accident and incident reports, and more, gathered from public sources and published as parquet datasets.

## Datasets

Every dataset is published in two layers, under the same keys locally (`data/`) and in the R2 bucket `plane-text`:

- `raw/<dataset>/v<snapshot>/<file>.parquet`: each source as delivered, converted to parquet. The source's own field names, every value as text, nothing cleaned, decoded or dropped (except personal data, below), plus provenance. Everything downstream can be rebuilt from raw alone.
- `processed/<dataset>/v<snapshot>/<file>.parquet`: the user-facing tables, built from raw only: cleaned, typed, one row per real-world entity, with codes decoded to labels beside them. Provenance adds `built_from` (the raw keys) and `built_at`.

Each parquet file carries its provenance (source, source URL, snapshot, retrieval time) as `plane_text.*` key-value metadata. Source downloads are cached under `data/cache/`, so re-runs only re-parse.

| Dataset | Source | Raw | Processed |
| --- | --- | --- | --- |
| `ecfr-title14` | 14 CFR, eCFR versioner API | `structure` (one row per structure node), `parts` (one row per part, XML as delivered) | `sections`: one row per section or appendix |
| `faa-ads` | FAA AD final rules (`ADFRAWD`), DRS API via [`faa-drs`](https://pypi.org/project/faa-drs/) | every DRS record as read, API field names | `ad-final-rules`: one row per DRS document (corrections are separate rows) |
| `faa-proposed-rules` | FAA AD proposed rules (`ADNPRM`) and NPRMs (`NPRM`), DRS; Federal Register API | DRS records as read; Federal Register texts as fetched | `ad-proposed-rules`, `proposed-rules`: one row per DRS document |
| `faa-saibs` | Special Airworthiness Information Bulletins (`SAIB`), DRS PDFs | `documents` (DRS records), `document-pages` (one row per PDF page: text layer, OCR or pending OCR) | `documents`: one row per bulletin with its text |
| `faa-advisory-circulars` | Advisory circulars (`AC`), DRS PDFs | as SAIBs | `documents`: one row per circular with its text |
| `faa-maintenance-alerts` | Aviation Maintenance Alerts (`ALERTS`, the monthly AC 43-16A issues DRS holds, 2004–2012), DRS PDFs | as SAIBs | `documents`: one row per monthly alert with its text |
| `faa-sdrs` | FAA Service Difficulty Reports, yearly CSVs, 1995 onward | every row of every yearly CSV, the FAA's 76 columns | `service-difficulty-reports`: one row per report, codes decoded |
| `faa-sdrs-lookups` | FAA SDRS Look-Up Tables and the 2008 JASC code table | each workbook as delivered, and the JASC PDF's text lines | one cleaned table per code set, plus `jasc` |
| `ntsb` (raw only) | NTSB accident databases `avall.zip` (2008 onward) and `Pre2008.zip` (1982–2007) | `<db>-<table>`: each Access table as exported, `<db>` is `avall` or `Pre2008` | (see the three below) |
| `ntsb-narratives` | `raw/ntsb` | | `narratives`: one row per aircraft per event, codes decoded |
| `ntsb-findings` | `raw/ntsb` | | `findings`, `events_sequence` (2008 on), `occurrences`, `sequence_of_events` (1982–2007), codes decoded |
| `ntsb-lookups` | `raw/ntsb` (embedded data dictionary and code tables) | | `columns`, `codes`, `code_sets`, `sequence_of_events_codes`, `countries` |

In processed tables, every code column `x_code` is followed by its label `x`; a code with no entry in the lookups keeps its code and gets a null label. Values that fail to parse when typed become null, are counted in the `plane_text.unparsed` metadata, and keep their original text in raw. Label coverage per column is recorded in `plane_text.label_coverage` where computed.

Personal data: NTSB owner and operator names and street addresses are withheld already at raw (listed in `plane_text.withheld_columns`); they remain only in the local download cache.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

## Running a pipeline

Every pipeline takes `--snapshot` (the version date, default today; for eCFR the issue date), `--step raw|processed|all` (default all, raw first), `--data-dir` (default `data`) and `--upload`.

```sh
uv run python -m plane_text.ecfr                          # latest eCFR issue
uv run python -m plane_text.faa_ads
uv run python -m plane_text.faa_proposed_rules
uv run python -m plane_text.faa_guidance SAIB AC ALERTS --no-ocr # --no-ocr: use cached OCR, leave other scans pending
uv run python -m plane_text.faa_sdrs_lookups              # before faa_sdrs: processed SDRs read the processed lookups
uv run python -m plane_text.faa_sdrs
uv run python -m plane_text.ntsb_lookups                  # before ntsb and ntsb_findings, for the same reason
uv run python -m plane_text.ntsb
uv run python -m plane_text.ntsb_findings
uv run python -m plane_text.faa_sdrs --step processed --snapshot 2026-10-23   # rebuild processed from raw
```

Caches live under `data/cache/<source>/`: DRS under `drs/<doctype>/<snapshot>/`, NTSB under `ntsb/`, Federal Register texts under `federal-register/`. To finish OCR of scanned PDFs later, run `uv run python -m plane_text.pdf_text data/cache/drs/AC/<snapshot>/files`, then rebuild the AC raw step.

Requirements beyond Python:

- R2 uploads: `S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The bucket defaults to `plane-text`; override it with `PLANETEXT_BUCKET`.
- DRS pipelines (ADs, proposed rules, SAIBs, ACs, maintenance alerts): `DRS_API_KEY`, for anything not yet cached. Federal Register texts need access to `www.federalregister.gov`.
- SAIB, AC and maintenance alert raw steps: `pdftotext` and `pdftoppm` (poppler-utils), and `tesseract` unless `--no-ocr` is given.
- NTSB raw steps: `mdb-export` from [mdbtools](https://github.com/mdbtools/mdbtools) (`apt-get install mdbtools`).

Processed steps need none of these: they read raw parquet only.

Every pipeline but the advisory circulars also runs on Cloudflare: a Worker starts it in a Container, which uploads to R2 the same way. See `pipelines/cloudflare/README.md`.

## Releases

The pipelines keep refreshing `processed/` under dated snapshots. A release freezes one snapshot of every processed dataset (no raw data) as a numbered zip, `plane-text-v<major>.<minor>.zip`: what the site's download serves, and what a Hugging Face dataset repo will hold (unzipped). Releases are cut in GitHub, in two steps:

1. **Pin** the snapshots, and merge them through a pull request:

   ```sh
   uv run python -m plane_text.release pin 1.1      # writes releases/v1.1.json from R2's newest snapshots
   git diff --no-index releases/v1.0.json releases/v1.1.json   # what changed since the last release
   ```

   The pin file lists every object with its size and ETag. CI checks that each pin file is well formed.

2. **Tag** the merge commit on `main` and push the tag:

   ```sh
   git tag v1.1 <commit on main> && git push origin v1.1
   ```

   The Release workflow (`.github/workflows/release.yml`) builds exactly the pinned objects, refusing any that changed in R2 since they were pinned. It publishes `releases/plane-text-v1.1.zip` to R2, points `releases/latest.json` at it, and creates the GitHub release `v1.1` with generated notes, the manifest and the README attached. It runs in the `release` environment, which holds the R2 secrets (`S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) and can require an approval.

Versions only go up: a minor bump for a refresh, a major one for a change in what the corpus is. Pinning or publishing at or below a published version is refused. To build a release locally without publishing: `uv run python -m plane_text.release build releases/v1.1.json` (into `data/releases/`).

The zip:

```
plane-text-v<version>/
  README.md        dataset card: contents, layout, provenance, license, citation; its YAML front matter
                   declares each table as a Hugging Face config
  LICENSE          CC BY 4.0
  manifest.json    every file: dataset, table, snapshot, rows, columns, bytes, sha256, source key
  data/<dataset>/<table>.parquet
```

Parquet is stored in the zip uncompressed (it already is, with zstd). Releases are licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the underlying texts are US federal government works.

## Site

The landing page lives in `site/`: Astro with Svelte and TypeScript, served as static assets on Cloudflare Workers at `plane-text.cldixon.dev` (the old `planetext.cldixon.dev` redirects there). Requires [Bun](https://bun.sh); Astro runs on the Bun runtime.

```sh
cd site
bun install && bun install --cwd cloudflare
bun run dev       # Astro dev server
bun run build     # type-check, then build to site/dist
bun run preview   # build, then serve the built site locally with cf dev
bun run deploy    # build, then deploy with cf
bun run deploy:preview   # build, then upload a version (not deployed) with a preview URL named after the branch
```

Deployment uses Cloudflare's [`cf`](https://github.com/cloudflare/cf) CLI (beta), configured in `site/cloudflare/cloudflare.config.ts`. It is a separate project inside `site/` because `cf` does not yet build Astro projects directly: Astro builds `site/dist`, and `cf` packages that directory as static assets. `cf` itself runs on Node 22.18 or later, since it cannot load `cloudflare.config.ts` on Bun yet. Deploying needs `CLOUDFLARE_API_TOKEN`. A preview lives at `https://<branch>-plane-text.cl-dixon.workers.dev` (the branch name after its last `/`); production is served only on the custom domain.

Deploys run in GitHub Actions (`.github/workflows/site.yml`) whenever the site changes: a push to `main` deploys to production, and a pull request gets a preview version (uploaded, not deployed) at `https://pr-<number>-plane-text.cl-dixon.workers.dev`, posted as a comment on the pull request. The workflow needs the repository secrets `CLOUDFLARE_API_TOKEN` (a token from Cloudflare's "Edit Cloudflare Workers" template, for this account and the `cldixon.dev` zone) and `CLOUDFLARE_ACCOUNT_ID`; without them it skips the deploy with a warning. `bun run deploy` and `bun run deploy:preview` still work by hand.

The site is a single page laid out like a paper: the title, a tagline and link badges, then the abstract, a data plate and charts of the corpus. Top right, Download fetches the latest release (`/download`, which the Worker redirects to `/download/plane-text-v<version>.zip`, served from R2), beside a dark/light theme toggle (dark by default). A Query button floats in the bottom right corner (also Cmd/Ctrl+K, or a link to `/#shell`; `/shell` redirects there): it opens an in-browser SQL shell in a modal over the page. The shell runs DuckDB-WASM (loaded from jsDelivr on the first open only) over the airworthiness directives parquet, which a small Worker serves from the private R2 bucket at `/data/*` with HTTP Range requests. The console's look comes from its own `--console-*` design tokens. The corpus figures live in `site/src/corpus.ts`. See `site/cloudflare/README.md`, including how to seed the local R2 bucket for `bun run preview`.

Design tokens (colors, type, space) live in `site/src/design/tokens.css`; see `site/src/design/README.md`.
