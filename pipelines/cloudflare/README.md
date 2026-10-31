# Pipelines on Cloudflare

Runs the `plane_text` pipelines in a [Cloudflare Container](https://developers.cloudflare.com/containers/), which writes the raw and processed parquet straight to R2 with the same `--upload` path as a local run.

| Pipeline | Steps, in one container | Instance | Time limit |
| --- | --- | --- | --- |
| `ecfr-title14` | `ecfr` | `standard-1` (½ vCPU, 4 GiB) | 1 h |
| `faa-ads` | `faa_ads` | `standard-1` | 1 h |
| `faa-proposed-rules` | `faa_proposed_rules` | `standard-1` | 2 h |
| `faa-sdrs` | `faa_sdrs_lookups`, then `faa_sdrs` | `standard-4` (4 vCPU, 12 GiB, 20 GB disk); decoding peaks near 8 GiB | 1 h |
| `ntsb` | `ntsb_lookups`, then `ntsb`, then `ntsb_findings` | `standard-3` (2 vCPU, 8 GiB) | 1 h |
| `faa-saibs` | `faa_guidance SAIB --workers 4` | `standard-4` | 1 h |
| `faa-maintenance-alerts` | `faa_guidance ALERTS --workers 4` | `standard-4` | 1 h |

Each step is `python -m plane_text.<step> --upload --snapshot <date>`, all with the same date (the request's `snapshot`, or today in UTC), stopping at the first step that fails. Steps share the container's disk, so a later step reads what an earlier one built: processed SDRs and NTSB tables decode their codes with the lookups built just before them.

Advisory circulars are not run here: every run would re-download 2.5 GB of PDFs and OCR about 7,000 scanned pages, hours of work that is almost all repeated. They need extraction state that outlives the container first.

The PDF pipelines download every PDF and OCR every scanned page on each run (nothing is cached between runs yet), one Tesseract per vCPU.

- `Dockerfile`: Ubuntu 24.04 with poppler-utils, Tesseract (English) and mdbtools, uv, the Python 3.13 that uv installs, the locked dependencies and `src/`. Built from the repository root (`.dockerignore` there keeps the context to `pyproject.toml`, `uv.lock`, `.python-version`, `README.md` and `src/`).
- `cloudflare.config.ts`: the Worker `plane-text-pipelines`, the Container image, the `PipelineRun` Durable Object that owns the container, and the secrets.
- `src/index.ts`: the run endpoints and `PipelineRun`. One Durable Object per pipeline, so a pipeline runs at most once at a time. It starts the container (internet on) on the pipeline's instance type with the pipeline's command as entrypoint, records the run, and marks it succeeded or failed when the container exits. Every minute an alarm renews the container's inactivity timeout, re-attaches the wait on the container if the object restarted (after a deploy, say), records a container that vanished without an exit, and stops a run that passes its time limit.

The inactivity timeout matters for long runs: Cloudflare stops a container soon after its Durable Object goes idle, and a pending `monitor()` keeps the object busy for only 15 minutes. Without the timeout, the first advisory circular run was stopped 32 minutes in, its extraction workers killed mid-OCR.

## Running

```sh
curl -X POST -H "Authorization: Bearer $RUN_TOKEN" https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads
curl -X POST -H "Authorization: Bearer $RUN_TOKEN" "https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads?snapshot=2026-10-27"
curl -H "Authorization: Bearer $RUN_TOKEN" https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads   # latest 20 runs
curl -X DELETE -H "Authorization: Bearer $RUN_TOKEN" https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads   # stop the current run
```

`POST` answers 202 with the new run, or 409 with the current one if the pipeline is already running. `DELETE` records the current run as cancelled and destroys its container. Right after a deploy, a Durable Object still running the previous code can answer 500 until it restarts; retry. Container output (the pipeline's own log) is in the Worker's observability logs.

To add a pipeline, add it to `PIPELINES` in `src/index.ts`, and any secret it needs to the config and to the container's `env`. A pipeline needing other system tools needs them in the `Dockerfile`.

## Deploying

Needs Docker, `CLOUDFLARE_API_TOKEN` (Workers and Containers), and Node 22.18 or later for `cf`.

```sh
bun install
bun run check                                          # binding types, then tsc
bunx cf deploy --secrets-file <path/to/secrets.env>    # builds and pushes the image, then deploys
```

The secrets file (`.env` format, never committed) holds `RUN_TOKEN`, `DRS_API_KEY`, `S3_API_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Without `--secrets-file`, a deploy keeps the secrets already set; change one with `bunx cf workers secrets update`.

Behind a TLS-intercepting proxy, the image build can trust the proxy's CA through an optional build secret, `docker build --secret id=ca,src=<ca.pem>`, which is used during the build only and never stored in the image; apt then switches to HTTPS, since such proxies usually refuse plain HTTP. The base image comes from `mirror.gcr.io` (Google's Docker Hub mirror), and Python from uv rather than a Debian-based `python` image, because the environment this was first built in reached Ubuntu's package mirrors but not Debian's.

## Not yet

- A schedule (a Workflow with a cron, or a Cron Trigger).
- Skipping a run when the source has not changed: every run is a full fetch and a new snapshot, even when identical to the last one.
- Download caches that outlive the container, which PDF-heavy pipelines (SAIBs, ACs) need.
