"""History-depth probe (strategy step 1).

Question: what does the incremental prices endpoint return for
effective-start-timestamp = 2026-02-02? Only the latest price per station + fuel, or a full
change log? Every response is saved to the raw store; a JSON summary is written for
docs/history-depth.md.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from milemap.config import data_dir, fuel_finder_settings, raw_root
from milemap.sources.fuel_finder.client import (
    PRICES_PATH,
    STATIONS_PATH,
    FuelFinderClient,
    PaginationResult,
)
from milemap.sources.fuel_finder.errors import FuelFinderError, GeoBlockedError
from milemap.storage.raw import RawRunWriter

log = logging.getLogger(__name__)

SOURCE = "fuel_finder"
LAUNCH_DATE = "2026-02-02"
FORMAT_TESTS = ("{d}", "{d} 00:00:00", "{d}T00:00:00Z")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def flatten(record: Any) -> list[dict[str, Any]]:
    """One row per (station, fuel) price entry. Handles nested and flat shapes."""
    if not isinstance(record, dict):
        return []
    node = record.get("node_id")
    prices = record.get("fuel_prices")
    if isinstance(prices, list):
        return [{"node_id": node, **p} for p in prices if isinstance(p, dict)]
    if "fuel_type" in record:
        return [record]
    return []


@dataclass
class RunStats:
    label: str
    params: dict[str, Any]
    records: int = 0
    price_rows: int = 0
    batches: int = 0
    stopped_because: str = ""
    seconds: float = 0.0
    record_keys: Counter = field(default_factory=Counter)
    price_keys: Counter = field(default_factory=Counter)
    envelope_keys: Counter = field(default_factory=Counter)
    fuel_types: Counter = field(default_factory=Counter)
    nodes: set = field(default_factory=set)
    node_records: Counter = field(default_factory=Counter)
    pair_effs: dict = field(default_factory=lambda: defaultdict(set))
    exact_rows: Counter = field(default_factory=Counter)
    null_counts: Counter = field(default_factory=Counter)
    min_eff: datetime | None = None
    max_eff: datetime | None = None
    min_upd: datetime | None = None
    max_upd: datetime | None = None
    eff_before_since: int = 0
    price_min: float | None = None
    price_max: float | None = None
    price_out_of_range: int = 0
    price_types: Counter = field(default_factory=Counter)
    ts_samples: list = field(default_factory=list)
    raw_sample: Any = None
    batch_sizes: Counter = field(default_factory=Counter)
    manifest: str | None = None
    error: str | None = None
    since_dt: datetime | None = None

    def add_batch(self, records: list[Any], envelope: dict[str, Any]) -> None:
        self.batches += 1
        self.batch_sizes[len(records)] += 1
        self.envelope_keys.update(envelope.keys())
        for rec in records:
            self.records += 1
            if self.raw_sample is None:
                self.raw_sample = rec
            if isinstance(rec, dict):
                self.record_keys.update(rec.keys())
                if rec.get("node_id") is not None:
                    self.node_records[rec["node_id"]] += 1
            for row in flatten(rec):
                self._add_row(row)

    def _add_row(self, row: dict[str, Any]) -> None:
        self.price_rows += 1
        self.price_keys.update(row.keys())
        node, fuel = row.get("node_id"), row.get("fuel_type")
        eff_raw = row.get("price_change_effective_timestamp")
        upd_raw = row.get("price_last_updated")
        price = row.get("price")
        for k in ("node_id", "fuel_type", "price", "price_change_effective_timestamp"):
            if row.get(k) in (None, ""):
                self.null_counts[k] += 1
        self.nodes.add(node)
        self.fuel_types[fuel] += 1
        self.pair_effs[(node, fuel)].add(eff_raw)
        self.exact_rows[(node, fuel, eff_raw, str(price), upd_raw)] += 1
        if len(self.ts_samples) < 3:
            self.ts_samples.append({"price_change_effective_timestamp": eff_raw,
                                    "price_last_updated": upd_raw, "price": price})
        self.price_types[type(price).__name__] += 1
        try:
            p = float(price)
            self.price_min = p if self.price_min is None else min(self.price_min, p)
            self.price_max = p if self.price_max is None else max(self.price_max, p)
            if not 100 <= p <= 300:
                self.price_out_of_range += 1
        except (TypeError, ValueError):
            pass
        eff, upd = _parse_ts(eff_raw), _parse_ts(upd_raw)
        if eff:
            self.min_eff = eff if self.min_eff is None else min(self.min_eff, eff)
            self.max_eff = eff if self.max_eff is None else max(self.max_eff, eff)
            if self.since_dt and eff < self.since_dt:
                self.eff_before_since += 1
        if upd:
            self.min_upd = upd if self.min_upd is None else min(self.min_upd, upd)
            self.max_upd = upd if self.max_upd is None else max(self.max_upd, upd)

    def summary(self, *, samples: int = 0) -> dict[str, Any]:
        per_pair = Counter(len(v) for v in self.pair_effs.values())
        dup_rows = sum(c - 1 for c in self.exact_rows.values() if c > 1)
        multi_pairs = sum(1 for v in self.pair_effs.values() if len(v) > 1)
        out: dict[str, Any] = {
            "label": self.label,
            "params": self.params,
            "batches": self.batches,
            "stopped_because": self.stopped_because,
            "seconds": round(self.seconds, 1),
            "records": self.records,
            "price_rows": self.price_rows,
            "distinct_nodes": len(self.nodes),
            "nodes_appearing_in_more_than_one_record": sum(
                1 for c in self.node_records.values() if c > 1),
            "distinct_node_fuel_pairs": len(self.pair_effs),
            "distinct_node_fuel_effective_ts": sum(len(v) for v in self.pair_effs.values()),
            "pairs_with_multiple_effective_ts": multi_pairs,
            "effective_ts_per_pair_histogram": dict(sorted(per_pair.items())[:15]),
            "exact_duplicate_price_rows": dup_rows,
            "min_effective_ts": self.min_eff.isoformat() if self.min_eff else None,
            "max_effective_ts": self.max_eff.isoformat() if self.max_eff else None,
            "min_price_last_updated": self.min_upd.isoformat() if self.min_upd else None,
            "max_price_last_updated": self.max_upd.isoformat() if self.max_upd else None,
            "rows_with_effective_ts_before_since": self.eff_before_since,
            "fuel_types": dict(self.fuel_types.most_common()),
            "price_min": self.price_min,
            "price_max": self.price_max,
            "price_value_types": dict(self.price_types),
            "price_rows_outside_100_300p": self.price_out_of_range,
            "null_counts": dict(self.null_counts),
            "record_keys": dict(self.record_keys.most_common()),
            "price_entry_keys": dict(self.price_keys.most_common()),
            "envelope_keys": dict(self.envelope_keys),
            "batch_size_histogram": dict(self.batch_sizes.most_common(5)),
            "timestamp_samples": self.ts_samples,
            "manifest": self.manifest,
            "error": self.error,
        }
        if samples and multi_pairs:
            busiest = sorted(self.pair_effs.items(), key=lambda kv: -len(kv[1]))[:samples]
            out["busiest_pairs"] = [
                {"node_id": k[0], "fuel_type": k[1], "distinct_effective_ts": len(v),
                 "first_10_effective_ts": sorted(x for x in v if x)[:10]}
                for k, v in busiest
            ]
        return out


class Probe:
    def __init__(self, client: FuelFinderClient, root: Path):
        self.c = client
        self.root = root
        self.results: dict[str, Any] = {"runs": []}

    def run(self, label: str, endpoint: str, params: dict[str, Any], *,
            max_batches: int | None, samples: int = 0, since_dt: datetime | None = None,
            path: str = PRICES_PATH, stats: Any = None) -> Any:
        if stats is None:
            stats = RunStats(label=label, params=params, since_dt=since_dt)
        writer = RawRunWriter(self.root, SOURCE, endpoint)
        pag = PaginationResult()
        t0 = datetime.now(UTC)
        status = "ok"
        log.info("▶ %s  params=%s  cap=%s", label, params, max_batches)
        try:
            for b in self.c.iter_batches(path, params, max_batches=max_batches,
                                         result=pag):
                writer.add_batch(number=b.number, records=b.records, path=b.path,
                                 params=b.params, fetched_at=b.fetched_at, status=b.status,
                                 envelope=b.envelope, elapsed_s=b.elapsed_s,
                                 attempts=b.attempts)
                stats.add_batch(b.records, b.envelope)
                if b.number % 25 == 0:
                    log.info("   … batch %d, %d records so far", b.number, stats.records)
        except GeoBlockedError:
            writer.close(status="failed", extra={"probe_label": label, "error": "HTTP 403"})
            raise
        except FuelFinderError as e:
            status = "failed"
            stats.error = str(e)
            log.error("   ✗ %s failed: %s", label, e)
        stats.stopped_because = pag.stopped_because or ("error" if stats.error else "")
        stats.seconds = (datetime.now(UTC) - t0).total_seconds()
        mpath = writer.close(status=status, extra={
            "probe_label": label, "request_params": params,
            "stopped_because": stats.stopped_because, "error": stats.error,
        })
        stats.manifest = str(mpath.relative_to(self.root))
        log.info("   ✓ %s: %d batches, %d records, %d price rows, stop=%s, %.0fs",
                 label, stats.batches, stats.records, stats.price_rows,
                 stats.stopped_because, stats.seconds)
        self.results["runs"].append(stats.summary(samples=samples))
        return stats


def run_probe(*, quick: bool, max_batches_big: int = 6000, max_batches_window: int = 400
              ) -> Path:
    settings = fuel_finder_settings()
    client = FuelFinderClient(settings)
    root = raw_root()
    probe = Probe(client, root)
    started = datetime.now(UTC)
    probe.results.update({
        "started_at_utc": started.isoformat(),
        "mode": "quick" if quick else "full",
        "base_url": settings.base_url,
        "raw_root": str(root),
    })
    yday = (started - timedelta(days=1)).date().isoformat()

    try:
        # 1. Auth: token generated once and reused for everything below.
        client.tokens.token()
        probe.results["auth"] = client.tokens.response_shape

        # 2. Which effective-start-timestamp formats does the server accept? (batch 1 only)
        fmt_results = []
        for fmt in FORMAT_TESTS:
            value = fmt.format(d=yday)
            s = probe.run(f"format test: {value!r}", "probe_format_tests",
                          {"effective-start-timestamp": value}, max_batches=1)
            fmt_results.append({"value": value, "error": s.error, "records_batch1": s.records,
                                "price_rows_batch1": s.price_rows,
                                "min_effective_ts": s.summary()["min_effective_ts"]})
        probe.results["format_tests"] = fmt_results

        # 3. Full prices, batch 1 only in quick mode
        probe.run("full prices", "prices_full", {}, max_batches=1 if quick else None)

        if not quick:
            ok = [f["value"] for f in fmt_results if not f["error"]]
            dt_ok = next((v for v in ok if " " in v or "T" in v), None)

            # 4. Time precision: does a time-of-day filter actually narrow results?
            if dt_ok:
                sep = "T" if "T" in dt_ok else " "
                suffix = "Z" if dt_ok.endswith("Z") else ""
                for hours in (1, 6):
                    since = (started - timedelta(hours=hours)).replace(microsecond=0)
                    value = since.strftime(f"%Y-%m-%d{sep}%H:%M:%S") + suffix
                    probe.run(f"time precision: last {hours}h", "prices_incremental",
                              {"effective-start-timestamp": value},
                              max_batches=max_batches_window, since_dt=since)

            date_fmt = next((v for v in ok if " " not in v and "T" not in v), None)
            use_fmt = "{d}" if date_fmt else ("{d} 00:00:00" if dt_ok and " " in dt_ok
                                              else "{d}T00:00:00Z")

            # Control: a future date. A working filter should return nothing.
            tomorrow = (started + timedelta(days=1)).date().isoformat()
            probe.run("control: since tomorrow", "prices_incremental",
                      {"effective-start-timestamp": use_fmt.format(d=tomorrow)},
                      max_batches=max_batches_window)

            # 5. Date windows
            for days in (1, 7, 30, 90):
                d = (started - timedelta(days=days)).date()
                probe.run(f"window: since {days}d ago", "prices_incremental",
                          {"effective-start-timestamp": use_fmt.format(d=d.isoformat())},
                          max_batches=max_batches_window,
                          since_dt=datetime(d.year, d.month, d.day, tzinfo=UTC))

            # 6. The main question: everything since launch
            probe.run(f"since launch {LAUNCH_DATE}", "prices_incremental",
                      {"effective-start-timestamp": use_fmt.format(d=LAUNCH_DATE)},
                      max_batches=max_batches_big, samples=5,
                      since_dt=datetime(2026, 2, 2, tzinfo=UTC))
    except GeoBlockedError as e:
        probe.results["fatal"] = f"GeoBlocked: {e}"
        log.error("✗ %s", e)
    except FuelFinderError as e:
        probe.results["fatal"] = str(e)
        log.error("✗ %s", e)

    probe.results.update({
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "requests": client.request_count,
        "retries": client.retry_count,
        "tokens_generated": client.tokens.generate_count,
        "tokens_refreshed": client.tokens.refresh_count,
    })
    out_dir = data_dir() / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"history_depth_{probe.results['mode']}_{started:%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(probe.results, indent=2, default=str))
    log.info("Summary written to %s", out)
    return out


# ── Stations (PFS information) ──────────────────────────────────────────────


def _is_ts(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 10 and value[4:5] == "-" and (
        _parse_ts(value) is not None)


def _field_paths(rec: dict[str, Any]) -> list[tuple[str, Any]]:
    """Top-level fields plus one nested level (dicts as a.b, lists of dicts as a[].b)."""
    out: list[tuple[str, Any]] = []
    for k, v in rec.items():
        out.append((k, v))
        if isinstance(v, dict):
            out.extend((f"{k}.{kk}", vv) for kk, vv in v.items())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    out.extend((f"{k}[].{kk}", vv) for kk, vv in item.items())
    return out


@dataclass
class StationStats:
    label: str
    params: dict[str, Any]
    since_dt: datetime | None = None
    records: int = 0
    price_rows: int = 0  # unused; keeps the shared log line simple
    batches: int = 0
    stopped_because: str = ""
    seconds: float = 0.0
    manifest: str | None = None
    error: str | None = None
    node_records: Counter = field(default_factory=Counter)
    node_versions: dict = field(default_factory=lambda: defaultdict(set))
    field_counts: Counter = field(default_factory=Counter)
    field_types: dict = field(default_factory=lambda: defaultdict(Counter))
    blank_counts: Counter = field(default_factory=Counter)
    bool_counts: dict = field(default_factory=lambda: defaultdict(Counter))
    ts_min: dict = field(default_factory=dict)
    ts_max: dict = field(default_factory=dict)
    ts_before_since: Counter = field(default_factory=Counter)
    envelope_keys: Counter = field(default_factory=Counter)

    def add_batch(self, records: list[Any], envelope: dict[str, Any]) -> None:
        self.batches += 1
        self.envelope_keys.update(envelope.keys())
        for rec in records:
            self.records += 1
            if not isinstance(rec, dict):
                self.field_types["<record>"][type(rec).__name__] += 1
                continue
            node = rec.get("node_id")
            self.node_records[node] += 1
            self.node_versions[node].add(json.dumps(rec, sort_keys=True))
            seen: set[str] = set()
            for path, v in _field_paths(rec):
                if path not in seen:
                    self.field_counts[path] += 1
                    seen.add(path)
                self.field_types[path][type(v).__name__] += 1
                if v is None or v == "" or v == []:
                    self.blank_counts[path] += 1
                if isinstance(v, bool):
                    self.bool_counts[path][v] += 1
                if _is_ts(v):
                    t = _parse_ts(v)
                    if path not in self.ts_min or t < self.ts_min[path]:
                        self.ts_min[path] = t
                    if path not in self.ts_max or t > self.ts_max[path]:
                        self.ts_max[path] = t
                    if self.since_dt and t < self.since_dt:
                        self.ts_before_since[path] += 1

    def summary(self, *, samples: int = 0) -> dict[str, Any]:
        versions = Counter(len(v) for v in self.node_versions.values())
        return {
            "label": self.label,
            "params": self.params,
            "batches": self.batches,
            "stopped_because": self.stopped_because,
            "seconds": round(self.seconds, 1),
            "records": self.records,
            "distinct_nodes": len(self.node_records),
            "nodes_appearing_in_more_than_one_record": sum(
                1 for c in self.node_records.values() if c > 1),
            "distinct_versions_per_node_histogram": dict(sorted(versions.items())),
            "fields_present_in_n_records": dict(self.field_counts.most_common()),
            "field_types": {k: dict(v) for k, v in self.field_types.items()},
            "blank_counts": dict(self.blank_counts.most_common()),
            "boolean_counts": {k: {str(b): n for b, n in v.items()}
                               for k, v in self.bool_counts.items()},
            "timestamp_fields": {
                k: {"min": self.ts_min[k].isoformat(), "max": self.ts_max[k].isoformat(),
                    "values_before_since": self.ts_before_since.get(k, 0)}
                for k in sorted(self.ts_min)
            },
            "envelope_keys": dict(self.envelope_keys),
            "manifest": self.manifest,
            "error": self.error,
        }


def _latest_full_nodes(root: Path, endpoint: str) -> tuple[str | None, set[str], dict]:
    """node_ids (and records) from the largest successful full run of an endpoint."""
    import pyarrow.parquet as pq

    best = None
    for m in (root / "_manifests" / f"source={SOURCE}" / f"endpoint={endpoint}").glob("*.json"):
        man = json.loads(m.read_text())
        if man.get("status") == "ok" and man.get("request_params") == {} and (
                best is None or man["rows_written"] > best["rows_written"]):
            best = man
    if best is None:
        return None, set(), {}
    recs: dict[str, Any] = {}
    for f in best["files"]:
        for text in pq.read_table(root / f["path"], columns=["record_json"]).column(
                "record_json").to_pylist():
            r = json.loads(text)
            if isinstance(r, dict) and r.get("node_id"):
                recs[r["node_id"]] = r
    return best["run_id"], set(recs), recs


def join_check(root: Path) -> dict[str, Any]:
    p_run, p_nodes, p_recs = _latest_full_nodes(root, "prices_full")
    s_run, s_nodes, s_recs = _latest_full_nodes(root, "stations_full")
    only_prices = sorted(p_nodes - s_nodes)
    only_stations = sorted(s_nodes - p_nodes)

    def describe(recs: dict, nodes: list[str], keys: tuple[str, ...]) -> list[dict]:
        out = []
        for n in nodes[:15]:
            r = recs.get(n, {})
            out.append({"node_id": n, **{k: r.get(k) for k in keys if k in r}})
        return out

    closure_keys = tuple(k for k in ("temporary_closure", "permanent_closure",
                                     "permanent_closure_date") if any(
        k in r for r in s_recs.values()))
    closure_summary: dict[str, Counter] = {k: Counter() for k in closure_keys}
    for n in only_stations:
        for k in closure_keys:
            closure_summary[k][str(s_recs[n].get(k))] += 1
    return {
        "prices_run": p_run, "stations_run": s_run,
        "price_nodes": len(p_nodes), "station_nodes": len(s_nodes),
        "in_both": len(p_nodes & s_nodes),
        "prices_without_station_record": len(only_prices),
        "stations_without_prices": len(only_stations),
        "examples_prices_without_station": describe(p_recs, only_prices, ("trading_name",)),
        "examples_stations_without_prices": describe(
            s_recs, only_stations, ("trading_name", *closure_keys)),
        "closure_flags_of_stations_without_prices": {
            k: dict(v) for k, v in closure_summary.items()},
    }


def run_station_probe(*, max_batches: int = 400) -> Path:
    settings = fuel_finder_settings()
    client = FuelFinderClient(settings)
    root = raw_root()
    probe = Probe(client, root)
    started = datetime.now(UTC)
    probe.results.update({"started_at_utc": started.isoformat(), "mode": "stations",
                          "base_url": settings.base_url})

    def st(label: str, params: dict[str, Any], endpoint: str, since: datetime | None = None):
        return probe.run(label, endpoint, params, max_batches=max_batches, path=STATIONS_PATH,
                         stats=StationStats(label=label, params=params, since_dt=since))

    try:
        st("stations full", {}, "stations_full")
        tomorrow = (started + timedelta(days=1)).date().isoformat()
        st("stations control: since tomorrow", {"effective-start-timestamp": tomorrow},
           "stations_incremental")
        six_h = (started - timedelta(hours=6)).replace(microsecond=0)
        st("stations: last 6h", {"effective-start-timestamp": six_h.strftime("%Y-%m-%d %H:%M:%S")},
           "stations_incremental", six_h)
        for days in (1, 7, 30, 90):
            d = (started - timedelta(days=days)).date()
            st(f"stations: since {days}d ago", {"effective-start-timestamp": d.isoformat()},
               "stations_incremental", datetime(d.year, d.month, d.day, tzinfo=UTC))
        st(f"stations: since launch {LAUNCH_DATE}", {"effective-start-timestamp": LAUNCH_DATE},
           "stations_incremental", datetime(2026, 2, 2, tzinfo=UTC))
        # Fresh full prices so the join compares two snapshots taken minutes apart.
        probe.run("full prices (for join)", "prices_full", {}, max_batches=None)
        probe.results["join"] = join_check(root)
    except FuelFinderError as e:
        probe.results["fatal"] = str(e)
        log.error("✗ %s", e)

    probe.results.update({
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "requests": client.request_count, "retries": client.retry_count,
        "tokens_generated": client.tokens.generate_count,
    })
    out_dir = data_dir() / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"history_depth_stations_{started:%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(probe.results, indent=2, default=str))
    log.info("Summary written to %s", out)
    return out
