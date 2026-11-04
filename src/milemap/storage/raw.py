"""Append-only, write-once raw store (local filesystem for now; R2 comes in the storage step).

Layout:
  {root}/source={source}/endpoint={endpoint}/ingest_date=YYYY-MM-DD/{run_id}_partNNNN.parquet
  {root}/_manifests/source={source}/endpoint={endpoint}/{run_id}.json

Every record is stored verbatim as JSON text, so later API schema changes never lose data.
Existing files are never overwritten: writes go to a temp file and are hard-linked into
place, which fails if the target already exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from milemap.config import REPO_ROOT

SCHEMA = pa.schema(
    [
        ("_run_id", pa.string()),
        ("_fetched_at_utc", pa.timestamp("us", tz="UTC")),
        ("_request_path", pa.string()),
        ("_request_params", pa.string()),
        ("_batch_number", pa.int32()),
        ("_record_index", pa.int32()),
        ("_record_hash", pa.string()),
        ("record_json", pa.string()),
    ]
)


class RawWriteError(RuntimeError):
    pass


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"run_{now:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def code_version() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or None
    except OSError:
        return None


def _write_once(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RawWriteError(f"Refusing to overwrite existing raw file: {path}")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(tmp)
        try:
            os.link(tmp, path)  # atomic; fails if path exists
        except FileExistsError:
            raise RawWriteError(f"Refusing to overwrite existing raw file: {path}") from None
    finally:
        tmp.unlink(missing_ok=True)


class RawRunWriter:
    def __init__(
        self,
        root: Path,
        source: str,
        endpoint: str,
        *,
        run_id: str | None = None,
        started_at: datetime | None = None,
        flush_every_rows: int = 50_000,
    ):
        self.root = Path(root)
        self.source, self.endpoint = source, endpoint
        self.started_at = started_at or datetime.now(UTC)
        self.run_id = run_id or new_run_id(self.started_at)
        self.dir = (
            self.root / f"source={source}" / f"endpoint={endpoint}"
            / f"ingest_date={self.started_at:%Y-%m-%d}"
        )
        self._rows: dict[str, list[Any]] = {n: [] for n in SCHEMA.names}
        self._flush_every = flush_every_rows
        self._part = 0
        self.files: list[dict[str, Any]] = []
        self.batches: list[dict[str, Any]] = []
        self.rows_written = 0
        self._closed = False

    def add_batch(self, *, number: int, records: list[Any], path: str, params: dict[str, Any],
                  fetched_at: datetime, status: int, envelope: dict[str, Any] | None = None,
                  elapsed_s: float | None = None, attempts: int | None = None) -> None:
        params_json = json.dumps(params, sort_keys=True)
        for i, rec in enumerate(records):
            text = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            self._rows["_run_id"].append(self.run_id)
            self._rows["_fetched_at_utc"].append(fetched_at)
            self._rows["_request_path"].append(path)
            self._rows["_request_params"].append(params_json)
            self._rows["_batch_number"].append(number)
            self._rows["_record_index"].append(i)
            self._rows["_record_hash"].append(hashlib.sha256(text.encode()).hexdigest())
            self._rows["record_json"].append(text)
        self.batches.append({
            "batch_number": number, "status": status, "records": len(records),
            "fetched_at_utc": fetched_at.isoformat(), "elapsed_s": elapsed_s,
            "attempts": attempts, "envelope": envelope or {},
        })
        if len(self._rows["_run_id"]) >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        n = len(self._rows["_run_id"])
        if n == 0:
            return
        table = pa.Table.from_pydict(self._rows, schema=SCHEMA)
        path = self.dir / f"{self.run_id}_part{self._part:04d}.parquet"
        _write_once(path, lambda tmp: pq.write_table(table, tmp, compression="zstd"))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.files.append({"path": str(path.relative_to(self.root)), "rows": n, "sha256": sha})
        self.rows_written += n
        self._part += 1
        self._rows = {k: [] for k in SCHEMA.names}

    def close(self, *, status: str, extra: dict[str, Any] | None = None) -> Path:
        if self._closed:
            raise RawWriteError("Run already closed")
        self.flush()
        manifest = {
            "run_id": self.run_id,
            "source": self.source,
            "endpoint": self.endpoint,
            "started_at_utc": self.started_at.isoformat(),
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "status": status,
            "rows_written": self.rows_written,
            "files": self.files,
            "batches": self.batches,
            "code_version": code_version(),
            **(extra or {}),
        }
        path = (
            self.root / "_manifests" / f"source={self.source}" / f"endpoint={self.endpoint}"
            / f"{self.run_id}.json"
        )
        _write_once(path, lambda tmp: tmp.write_text(json.dumps(manifest, indent=2, default=str)))
        self._closed = True
        return path
