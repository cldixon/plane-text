# Pipelines on Cloudflare

Runs the `plane_text` pipelines in a [Cloudflare Container](https://developers.cloudflare.com/containers/), which writes the raw and processed parquet straight to R2 with the same `--upload` path as a local run.

| Pipeline | Command | Instance | Time limit |
| --- | --- | --- | --- |
| `faa-ads` | `plane_text.faa_ads` | `standard-1` (½ vCPU, 4 GiB) | 1 h |
| `faa-saibs` | `plane_text.faa_guidance SAIB --workers 4` | `standard-4` (4 vCPU, 12 GiB, 20 GB disk) | 2 h |
| `faa-maintenance-alerts` | `plane_text.faa_guidance ALERTS --workers 4` | `standard-4` | 2 h |
| `faa-advisory-circulars` | `plane_text.faa_guidance AC --workers 4` | `standard-4` | 8 h |

The PDF pipelines download every PDF and OCR every scanned page on each run (nothing is cached between runs yet), one Tesseract per vCPU.

- `Dockerfile`: Ubuntu 24.04 with poppler-utils and Tesseract (English), uv, the Python 3.13 that uv installs, the locked dependencies and `src/`. Built from the repository root (`.dockerignore` there keeps the context to `pyproject.toml`, `uv.lock`, `.python-version`, `README.md` and `src/`).
- `cloudflare.config.ts`: the Worker `plane-text-pipelines`, the Container image, the `PipelineRun` Durable Object that owns the container, and the secrets.
- `src/index.ts`: the run endpoints and `PipelineRun`. One Durable Object per pipeline, so a pipeline runs at most once at a time. It starts the container (internet on) on the pipeline's instance type with the pipeline's command as entrypoint, records the run, and marks it succeeded or failed when the container exits. An alarm checks every minute as a backstop and stops a run that passes its time limit.

## Running

```sh
curl -X POST -H "Authorization: Bearer $RUN_TOKEN" https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads
curl -X POST -H "Authorization: Bearer $RUN_TOKEN" "https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads?snapshot=2026-10-27"
curl -H "Authorization: Bearer $RUN_TOKEN" https://plane-text-pipelines.cl-dixon.workers.dev/runs/faa-ads   # latest 20 runs
```

`POST` answers 202 with the new run, or 409 with the current one if the pipeline is already running. Container output (the pipeline's own log) is in the Worker's observability logs.

To add a pipeline, add it to `PIPELINES` in `src/index.ts`, and any secret it needs to the config and to the container's `env`. A pipeline needing other system tools (mdbtools for NTSB) needs them in the `Dockerfile`.

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
