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

## Site

The landing page lives in `site/`: Astro with Svelte and TypeScript, served as static assets on Cloudflare Workers at `planetext.cldixon.dev`. Requires [Bun](https://bun.sh); Astro runs on the Bun runtime.

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

The home page is a single page: overview, an in-browser SQL explorer, the datasets, how they are built, and status. The explorer runs DuckDB-WASM (loaded from jsDelivr) over the airworthiness directives parquet, which a small Worker serves from the private R2 bucket at `/data/*` with HTTP Range requests. See `site/cloudflare/README.md`, including how to seed the local R2 bucket for `bun run preview`.

Design tokens (colors, type, space) live in `site/src/design/tokens.css`; see `site/src/design/README.md`.
