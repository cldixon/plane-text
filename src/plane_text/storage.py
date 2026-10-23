"""Writing datasets to parquet and uploading them to Cloudflare R2.

Upload reads the S3-compatible R2 credentials from the environment:
S3_API_ENDPOINT, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. The bucket
defaults to `plane-text` and can be overridden with PLANETEXT_BUCKET.
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
import polars as pl

DEFAULT_BUCKET = "plane-text"


def write_parquet(frame: pl.DataFrame, path: Path, provenance: dict[str, str]) -> None:
    """Write `frame` with its provenance stored as parquet key-value metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {f"plane_text.{key}": value for key, value in provenance.items()}
    frame.write_parquet(path, compression="zstd", metadata=metadata)


def read_provenance(path: Path) -> dict[str, str]:
    metadata = pl.read_parquet_metadata(path)
    prefix = "plane_text."
    return {k.removeprefix(prefix): v for k, v in metadata.items() if k.startswith(prefix)}


def bucket() -> str:
    return os.environ.get("PLANETEXT_BUCKET", DEFAULT_BUCKET)


def object_key(path: Path, data_dir: Path) -> str:
    """The R2 key mirrors the path under the data directory."""
    return path.relative_to(data_dir).as_posix()


def upload(path: Path, data_dir: Path) -> str:
    key = object_key(path, data_dir)
    s3 = boto3.client("s3", endpoint_url=os.environ["S3_API_ENDPOINT"], region_name="auto")
    s3.upload_file(str(path), bucket(), key)
    return key
