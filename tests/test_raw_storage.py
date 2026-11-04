from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest

from milemap.storage.raw import RawRunWriter, RawWriteError, _write_once

T = datetime(2026, 11, 4, 20, 15, tzinfo=UTC)


def _write(tmp_path, run_id="run_x"):
    w = RawRunWriter(tmp_path, "fuel_finder", "prices_full", run_id=run_id, started_at=T)
    w.add_batch(number=1, records=[{"node_id": "a", "fuel_prices": []}, {"node_id": "b"}],
                path="/api/v1/pfs/fuel-prices", params={"batch-number": 1}, fetched_at=T,
                status=200)
    return w, w.close(status="ok")


def test_layout_roundtrip_and_manifest(tmp_path):
    w, manifest = _write(tmp_path)
    files = list(tmp_path.glob("source=fuel_finder/endpoint=prices_full/ingest_date=2026-11-04/*"))
    assert [f.name for f in files] == ["run_x_part0000.parquet"]
    t = pq.read_table(files[0])
    assert t.num_rows == 2
    assert t.column("record_json").to_pylist()[1] == '{"node_id":"b"}'
    assert manifest.exists() and '"rows_written": 2' in manifest.read_text()


def test_refuses_to_overwrite(tmp_path):
    _write(tmp_path)
    with pytest.raises(RawWriteError):
        _write(tmp_path)  # same run_id -> same file names


def test_write_once_helper(tmp_path):
    p = tmp_path / "f.txt"
    _write_once(p, lambda tmp: tmp.write_text("one"))
    with pytest.raises(RawWriteError):
        _write_once(p, lambda tmp: tmp.write_text("two"))
    assert p.read_text() == "one"
    assert [x.name for x in tmp_path.iterdir()] == ["f.txt"]  # no temp files left behind


def test_dedupe_skips_unchanged_records(tmp_path):
    seen: dict[str, str] = {}
    w = RawRunWriter(tmp_path, "fuel_finder", "prices_incremental", run_id="r1", started_at=T)
    recs = [{"node_id": "a", "p": 1}, {"node_id": "b", "p": 2}]
    w.add_batch(number=1, records=recs, path="/x", params={}, fetched_at=T, status=200,
                last_hash_by_key=seen)
    w.add_batch(number=2, records=[{"node_id": "a", "p": 1}, {"node_id": "b", "p": 3}],
                path="/x", params={}, fetched_at=T, status=200, last_hash_by_key=seen)
    w.close(status="ok")
    assert (w.records_returned, w.rows_written, w.skipped_unchanged) == (4, 3, 1)
