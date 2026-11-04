"""Upload the local outbox to Cloudflare R2, write-once.

Files are written locally first (data/outbox/<key>), so nothing is lost if the network or R2 is
down. Each upload uses `If-None-Match: *`, which makes R2 itself refuse to overwrite an existing
object. A file that already exists in R2 with the same SHA-256 counts as uploaded; one with
different content is a conflict and stays in the outbox (it is never overwritten).
After a successful upload the file moves to the local mirror (data/raw/<key>).
Data files are uploaded before manifests and run records, so a manifest in R2 implies its data
is there too.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from milemap.config import _load_env

log = logging.getLogger(__name__)


class R2ConfigError(RuntimeError):
    pass


@dataclass
class UploadResult:
    uploaded: int = 0
    already_present: int = 0
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    remaining: int = 0
    bytes: int = 0


def r2_client() -> tuple[Any, str]:
    import boto3
    from botocore.config import Config

    _load_env()
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    missing = [n for n in need if not os.environ.get(n)]
    if missing:
        raise R2ConfigError(f"Missing environment variables: {', '.join(missing)}")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2}),
    )
    return client, os.environ["R2_BUCKET"]


def _upload_order(paths: list[Path], root: Path) -> list[Path]:
    def rank(p: Path) -> tuple[int, str]:
        rel = p.relative_to(root).as_posix()
        if rel.startswith("_manifests/"):
            return (1, rel)
        if rel.startswith(("_runs/", "_gaps/")):
            return (2, rel)
        return (0, rel)
    return sorted(paths, key=rank)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_dir(
    src_root: Path,
    *,
    client: Any,
    bucket: str,
    move_to: Path | None,
    deadline: float | None = None,
    clock=time.monotonic,
) -> UploadResult:
    """Upload every file under src_root (key = relative path), oldest-first by kind."""
    from botocore.exceptions import ClientError

    res = UploadResult()
    if not src_root.exists():
        return res
    files = [p for p in src_root.rglob("*") if p.is_file() and not p.name.startswith(".")]
    for p in _upload_order(files, src_root):
        if deadline is not None and clock() > deadline:
            res.remaining += 1
            continue
        key = p.relative_to(src_root).as_posix()
        sha = _sha256(p)
        try:
            with p.open("rb") as f:
                client.put_object(Bucket=bucket, Key=key, Body=f, IfNoneMatch="*",
                                  Metadata={"sha256": sha})
            res.uploaded += 1
            res.bytes += p.stat().st_size
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412"):
                try:
                    head = client.head_object(Bucket=bucket, Key=key)
                    remote_sha = head.get("Metadata", {}).get("sha256")
                except ClientError:
                    remote_sha = None
                if remote_sha == sha:
                    res.already_present += 1
                else:
                    res.conflicts.append(key)
                    log.error("R2 already has a DIFFERENT object at %s; not overwriting", key)
                    continue
            else:
                res.errors.append(f"{key}: {code or type(e).__name__}")
                log.warning("R2 upload failed for %s: %s", key, code)
                res.remaining += 1
                continue
        except Exception as e:  # network errors etc.: keep in outbox, retry next run
            res.errors.append(f"{key}: {type(e).__name__}")
            log.warning("R2 upload failed for %s: %s", key, type(e).__name__)
            res.remaining += 1
            continue
        if move_to is not None:
            dest = move_to / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if _sha256(dest) != sha:
                    res.conflicts.append(f"local mirror differs: {key}")
                    continue
                p.unlink()
            else:
                shutil.move(str(p), dest)
    _prune_empty_dirs(src_root)
    return res


def _prune_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
