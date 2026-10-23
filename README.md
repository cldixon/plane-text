# plane-text

An open-source corpus of aviation maintenance text: regulations, airworthiness directives, accident and incident reports, and more, gathered from public sources and published as parquet datasets.

## Datasets

| Dataset | Source | Grain | Status |
| --- | --- | --- | --- |
| `ecfr-title14` | 14 CFR (Aeronautics and Space), eCFR versioner API | one row per section or appendix | in R2 |
| `faa-ads` | FAA AD final rules (`ADFRAWD`), DRS API via [`faa-drs`](https://pypi.org/project/faa-drs/) | one row per DRS document (corrections are separate rows) | in R2 |

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
```

Raw source files are cached under `data/raw/`, so re-runs only re-parse. Each parquet file carries its provenance (source, source URL, issue date, retrieval time) as `plane_text.*` key-value metadata.

Uploading needs R2's S3-compatible credentials in the environment: `S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The bucket defaults to `plane-text`; override it with `PLANETEXT_BUCKET`. The FAA ADs pipeline also needs `DRS_API_KEY`.
