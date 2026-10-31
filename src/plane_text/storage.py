"""Writing datasets to parquet and uploading them to Cloudflare R2.

Upload reads the S3-compatible R2 credentials from the environment:
S3_API_ENDPOINT, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. The bucket
defaults to `plane-text` and can be overridden with PLANETEXT_BUCKET.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import polars as pl

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

DEFAULT_BUCKET = "plane-text"

# Layers under the data directory (and in the bucket). `raw` holds each source
# as delivered, converted to parquet; `processed` holds the cleaned,
# user-facing tables built from raw. Downloads are cached under `cache`.
RAW = "raw"
PROCESSED = "processed"
CACHE = "cache"


def layer_path(data_dir: Path, layer: str, dataset: str, snapshot: str, name: str) -> Path:
    """`<data_dir>/<layer>/<dataset>/v<snapshot>/<name>.parquet`."""
    return data_dir / layer / dataset / f"v{snapshot}" / f"{name}.parquet"


def cache_dir(data_dir: Path, dataset: str) -> Path:
    """Where a dataset's source downloads are cached: `<data_dir>/cache/<dataset>`."""
    return data_dir / CACHE / dataset


def write_parquet(frame: pl.DataFrame, path: Path, provenance: dict[str, str]) -> None:
    """Write `frame` with its provenance stored as parquet key-value metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {f"plane_text.{key}": value for key, value in provenance.items()}
    frame.write_parquet(path, compression="zstd", metadata=metadata)


def read_provenance(path: Path) -> dict[str, str]:
    metadata = pl.read_parquet_metadata(path)
    prefix = "plane_text."
    return {k.removeprefix(prefix): v for k, v in metadata.items() if k.startswith(prefix)}


def derived_provenance(source: Path, data_dir: Path, **extra: str) -> dict[str, str]:
    """Provenance for a processed table: `source`'s own, plus its key and when it was built.

    `extra` overrides or adds entries (the layer, the table name, and so on).
    """
    built = {"built_from": object_key(source, data_dir), "built_at": now()}
    return read_provenance(source) | {"layer": PROCESSED} | built | extra


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def modified_at(*paths: Path) -> str:
    """When the newest of `paths` was written: the retrieval time of cached downloads."""
    latest = max(path.stat().st_mtime for path in paths)
    return datetime.fromtimestamp(latest, UTC).isoformat(timespec="seconds")


def bucket() -> str:
    return os.environ.get("PLANETEXT_BUCKET", DEFAULT_BUCKET)


def object_key(path: Path, data_dir: Path) -> str:
    """The R2 key mirrors the path under the data directory."""
    return path.relative_to(data_dir).as_posix()


def client() -> S3Client:
    return boto3.client("s3", endpoint_url=os.environ["S3_API_ENDPOINT"], region_name="auto")


def upload(path: Path, data_dir: Path, content_type: str | None = None) -> str:
    key = object_key(path, data_dir)
    extra = {"ContentType": content_type} if content_type else None
    client().upload_file(str(path), bucket(), key, ExtraArgs=extra)
    return key


def list_objects(prefix: str) -> dict[str, int]:
    """Every key in the bucket under `prefix`, with its size in bytes."""
    objects: dict[str, int] = {}
    for page in client().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        for item in page.get("Contents", []):
            objects[item["Key"]] = item["Size"]
    return objects


def download(key: str, data_dir: Path) -> Path:
    """Downloads `key` to the same path under the data directory."""
    path = data_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    client().download_file(bucket(), key, str(path))
    return path


def parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """The options every layered pipeline shares; `steps` lists the layers to build."""
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--snapshot",
        default=datetime.now(UTC).date().isoformat(),
        help="snapshot date (YYYY-MM-DD) naming the cache and the v<snapshot> versions; default today",
    )
    parser.add_argument(
        "--step",
        choices=[RAW, PROCESSED, "all"],
        default="all",
        help="build raw from the source, processed from raw, or both (default)",
    )
    parser.add_argument("--upload", action="store_true", help="upload what was built to R2")
    args = parser.parse_args()
    args.steps = [RAW, PROCESSED] if args.step == "all" else [args.step]
    return args
