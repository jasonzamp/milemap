"""The Fuel Finder collector: one `tick` every 5 minutes (launchd on the Mac, later Azure).

Each tick:
  1. takes a lock (runs never overlap), loads state;
  2. records a GAP if the previous run started more than 10 minutes ago (Mac asleep, offline,
     switched off...), with macOS sleep/wake evidence when available;
  3. respects a 429 cool-down (no API calls until it has passed);
  4. polls incremental prices (since now - 2 h, or further back to cover a gap) and incremental
     stations (since the start of the previous UTC day);
  5. once an hour adds a full prices download; once a day a full stations download
     (never both in the same tick, to keep the peak request count low);
  6. writes everything write-once to the local outbox, deduplicating unchanged station records,
     with one manifest per task, then uploads the outbox to R2;
  7. writes a run record (every run, including skipped and failed ones) so coverage and gaps are
     visible in the archive itself.
A hard deadline (45 s, or 120 s with a full download) bounds every run.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from milemap.config import data_dir, fuel_finder_settings
from milemap.sources.fuel_finder.client import (
    PRICES_PATH,
    STATIONS_PATH,
    FuelFinderClient,
    PaginationResult,
)
from milemap.sources.fuel_finder.errors import (
    DeadlineExceeded,
    FuelFinderError,
    GeoBlockedError,
    RateLimitedError,
)
from milemap.storage.raw import RawRunWriter, code_version, new_run_id, write_json_once

log = logging.getLogger(__name__)

SOURCE = "fuel_finder"
INTERVAL_S = 300
GAP_THRESHOLD_S = 600            # > 2 intervals without any run = gap
PRICES_LOOKBACK = timedelta(hours=2)
GAP_OVERLAP = timedelta(minutes=15)
MAX_CATCHUP = timedelta(hours=24)  # beyond this, rely on the full download instead
PRICES_FULL_EVERY = timedelta(minutes=55)
NORMAL_DEADLINE_S = 45
FULL_DEADLINE_S = 120
HARD_KILL_MARGIN_S = 10
UPLOAD_RESERVE_S = 8
COOLDOWN_S = 300
FULL_COUNT_TOLERANCE = 0.10


class HardTimeout(BaseException):  # noqa: N818 - BaseException so generic handlers can't swallow it
    """Raised by SIGALRM when a run exceeds its hard limit."""


@dataclass
class Paths:
    root: Path

    @property
    def outbox(self) -> Path: return self.root / "outbox"
    @property
    def mirror(self) -> Path: return self.root / "raw"
    @property
    def state_dir(self) -> Path: return self.root / "state"
    @property
    def state(self) -> Path: return self.state_dir / "fuel_finder_state.json"
    @property
    def token(self) -> Path: return self.state_dir / "fuel_finder_token.json"
    @property
    def lock(self) -> Path: return self.state_dir / "collector.lock"
    @property
    def runs_log(self) -> Path: return self.state_dir / "runs.jsonl"

    def hashes(self, kind: str) -> Path:
        return self.state_dir / f"last_hash_{kind}.json"


# ── small helpers ───────────────────────────────────────────────────────────

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, default=str))
    os.replace(tmp, path)


@contextmanager
def run_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def hard_limit(seconds: float) -> Iterator[None]:
    def _raise(signum, frame):
        raise HardTimeout(f"Run exceeded its hard limit of {seconds:.0f}s")
    old = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


_PMSET_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\s+(Sleep|Wake|DarkWake)\s")


def mac_sleep_events(start: datetime, end: datetime) -> list[dict[str, str]] | None:
    """Sleep/wake events from `pmset -g log` between start and end (macOS only, best effort)."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(  # noqa: S603
            ["/usr/bin/pmset", "-g", "log"], capture_output=True, text=True, timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    events = []
    for line in out.splitlines():
        m = _PMSET_LINE.match(line)
        if not m:
            continue
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
        if start <= t <= end:
            events.append({"at_utc": t.isoformat(), "event": m.group(2)})
    return events


def _fmt_since(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


# ── validation ──────────────────────────────────────────────────────────────

def validate(kind: str, records_returned: int, sample_stats: dict[str, Any],
             previous_full: int | None, full: bool) -> dict[str, Any]:
    checks = []
    req_missing = sample_stats.get("records_missing_required", 0)
    checks.append({"check": "required fields present", "passed": req_missing == 0,
                   "detail": f"{req_missing} records missing node_id"
                             + (" or fuel_prices" if kind == "prices" else "")})
    if kind == "prices" and sample_stats.get("price_rows"):
        share = sample_stats["price_rows_in_range"] / sample_stats["price_rows"]
        checks.append({"check": ">= 95% of prices within 100-300p", "passed": share >= 0.95,
                       "detail": f"{share:.1%}"})
    if full:
        if previous_full:
            change = abs(records_returned - previous_full) / previous_full
            checks.append({"check": "full count within ±10% of previous full",
                           "passed": change <= FULL_COUNT_TOLERANCE,
                           "detail": f"{records_returned} vs {previous_full} ({change:.1%})"})
        checks.append({"check": "full download not empty", "passed": records_returned > 0,
                       "detail": str(records_returned)})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


# ── the tick ────────────────────────────────────────────────────────────────

class Tick:
    def __init__(self, paths: Paths, runner: str):
        self.p = paths
        self.runner = runner
        self.state: dict[str, Any] = _load_json(paths.state, {})
        self.hashes = {k: _load_json(paths.hashes(k), {}) for k in ("prices", "stations")}
        self.started = datetime.now(UTC)
        self.mono0 = time.monotonic()
        self.run_id = new_run_id(self.started)
        self.record: dict[str, Any] = {
            "run_id": self.run_id, "source": SOURCE, "runner": runner,
            "started_at_utc": _iso(self.started), "status": "running", "tasks": [],
            "code_version": code_version(),
        }
        self.client: FuelFinderClient | None = None

    # gap detection --------------------------------------------------------
    def detect_gap(self) -> None:
        prev = _dt(self.state.get("last_run_started_at"))
        self.record["previous_run_started_at_utc"] = _iso(prev)
        if prev is None:
            self.record["note"] = "first run: archive coverage starts here"
            return
        gap_s = (self.started - prev).total_seconds()
        self.record["seconds_since_previous_run"] = round(gap_s)
        if gap_s <= GAP_THRESHOLD_S:
            return
        events = mac_sleep_events(prev, self.started)
        sleeps = [e for e in (events or []) if e["event"] == "Sleep"]
        cause = ("mac_asleep" if sleeps else
                 "no_run_mac_awake_or_unknown" if events is not None else "no_run_unknown")
        gap = {
            "source": SOURCE, "kind": "no_run",
            "from_utc": _iso(prev), "to_utc": _iso(self.started),
            "seconds": round(gap_s), "expected_interval_s": INTERVAL_S,
            "missed_runs_estimate": max(0, int(gap_s // INTERVAL_S) - 1),
            "cause": cause, "runner": self.runner,
            "sleep_wake_events": (events or [])[:200],
            "recovery": "next poll looks back to 15 min before the gap (max 24 h); "
                        "price changes superseded during the gap cannot be recovered",
            "detected_by_run": self.run_id,
        }
        rel = (f"_gaps/source={SOURCE}/"
               f"gap_{prev:%Y%m%dT%H%M%SZ}_{self.started:%Y%m%dT%H%M%SZ}.json")
        write_json_once(self.p.outbox, rel, gap)
        self.record["gap"] = {k: gap[k] for k in ("from_utc", "to_utc", "seconds", "cause")}
        log.warning("Gap of %.0f min since the previous run (%s)", gap_s / 60, cause)

    # plan -------------------------------------------------------------------
    def plan(self) -> dict[str, Any]:
        last_full = _dt(self.state.get("last_prices_full_at"))
        prices_full_due = last_full is None or self.started - last_full >= PRICES_FULL_EVERY
        today = self.started.date().isoformat()
        stations_full_due = (self.state.get("last_stations_full_date") != today
                             and not prices_full_due)
        since = self.started - PRICES_LOOKBACK
        last_ok_poll = _dt(self.state.get("last_prices_poll_ok_started_at"))
        if last_ok_poll and last_ok_poll - GAP_OVERLAP < since:
            since = last_ok_poll - GAP_OVERLAP
        if self.started - since > MAX_CATCHUP:
            since = self.started - MAX_CATCHUP
            prices_full_due = True
            stations_full_due = False
        complete_prices = self.state.get("last_complete_prices_date") != today
        yesterday = (self.started.date() - timedelta(days=1)).isoformat()
        return {"prices_since": since, "stations_since": yesterday,
                "prices_full": prices_full_due, "prices_full_complete": complete_prices,
                "stations_full": stations_full_due}

    # one endpoint -----------------------------------------------------------
    def run_task(self, name: str, path: str, params: dict[str, Any], *, kind: str,
                 full: bool, complete: bool) -> dict[str, Any]:
        assert self.client is not None
        writer = RawRunWriter(self.p.outbox, SOURCE, name, run_id=self.run_id,
                              started_at=self.started)
        hashes = self.hashes[kind]
        dedupe_map: dict[str, str] | None = {} if complete else hashes
        pag = PaginationResult()
        stats = {"records_missing_required": 0, "price_rows": 0, "price_rows_in_range": 0}
        nodes: set[str] = set()
        status, error = "ok", None
        t0 = time.monotonic()
        reqs0 = self.client.request_count
        try:
            for b in self.client.iter_batches(path, params, result=pag):
                for rec in b.records:
                    if not isinstance(rec, dict) or not rec.get("node_id") or (
                            kind == "prices" and not isinstance(rec.get("fuel_prices"), list)):
                        stats["records_missing_required"] += 1
                        continue
                    nodes.add(rec["node_id"])
                    if kind == "prices":
                        for fp in rec["fuel_prices"]:
                            stats["price_rows"] += 1
                            try:
                                if 100 <= float(fp.get("price")) <= 300:
                                    stats["price_rows_in_range"] += 1
                            except (TypeError, ValueError):
                                pass
                writer.add_batch(number=b.number, records=b.records, path=b.path,
                                 params=b.params, fetched_at=b.fetched_at, status=b.status,
                                 envelope=b.envelope, elapsed_s=b.elapsed_s,
                                 attempts=b.attempts, request_id=b.request_id,
                                 last_hash_by_key=dedupe_map)
        except RateLimitedError as e:
            status, error = "rate_limited", str(e)
            raise
        except DeadlineExceeded as e:
            status, error = "deadline", str(e)
            raise
        except GeoBlockedError as e:
            status, error = "geo_blocked", str(e)
            raise
        except FuelFinderError as e:
            status, error = "failed", str(e)
            log.error("%s failed: %s", name, e)
        except HardTimeout as e:
            status, error = "hard_timeout", str(e)
            raise
        finally:
            if complete and dedupe_map is not None and status == "ok":
                hashes.update(dedupe_map)
            prev_full = self.state.get("last_full_counts", {}).get(name) if full else None
            v = validate(kind, writer.records_returned, stats, prev_full, full)
            if status == "ok" and not v["passed"]:
                status = "suspect"
            summary = {
                "task": name, "status": status, "error": error, "params": params,
                "records_returned": writer.records_returned,
                "rows_written": None, "skipped_unchanged": writer.skipped_unchanged,
                "complete_snapshot": complete, "batches": pag.batches_fetched,
                "stopped_because": pag.stopped_because,
                "first_batch_404": pag.first_batch_404,
                "requests": self.client.request_count - reqs0,
                "seconds": round(time.monotonic() - t0, 1), "validation": v,
            }
            manifest = writer.close(status=status, extra={
                "request_params": params, "stopped_because": pag.stopped_because,
                "first_batch_404": pag.first_batch_404, "complete_snapshot": complete,
                "validation": v, "error": error, "collector_run_id": self.run_id,
            })
            summary["rows_written"] = writer.rows_written
            summary["manifest"] = manifest.relative_to(self.p.outbox).as_posix()
            if pag.first_batch_404:
                log.info("%s: batch 1 returned 404 (no changes, or endpoint problem)", name)
            self.record["tasks"].append(summary)
            log.info("%s: %s, %d returned, %d written, %d unchanged skipped, %d requests",
                     name, status, writer.records_returned, writer.rows_written,
                     writer.skipped_unchanged, summary["requests"])
        if status in ("ok", "suspect"):
            self._after_ok(name, kind, full, complete, nodes, writer.records_returned)
        return summary

    def _after_ok(self, name, kind, full, complete, nodes, returned) -> None:
        s = self.state
        key = f"current_{kind}_nodes"
        if full:
            s[key] = sorted(nodes)
            s.setdefault("last_full_counts", {})[name] = returned
            if kind == "prices":
                s["last_prices_full_at"] = _iso(self.started)
                if complete:
                    s["last_complete_prices_date"] = self.started.date().isoformat()
            else:
                s["last_stations_full_date"] = self.started.date().isoformat()
        else:
            s[key] = sorted(set(s.get(key, [])) | nodes)
            if kind == "prices":
                s["last_prices_poll_ok_started_at"] = _iso(self.started)

    def join_check(self) -> dict[str, Any]:
        p = set(self.state.get("current_prices_nodes", []))
        s = set(self.state.get("current_stations_nodes", []))
        if not p or not s:
            return {"available": False, "reason": "needs one full prices and stations download"}
        only_p, only_s = sorted(p - s), sorted(s - p)
        return {"available": True, "price_nodes": len(p), "station_nodes": len(s),
                "in_both": len(p & s),
                "prices_without_station_record": len(only_p),
                "stations_without_prices": len(only_s),
                "prices_without_station_record_ids": only_p[:50],
                "stations_without_prices_ids": only_s[:50]}

    # whole run ---------------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        self.detect_gap()
        cooldown = _dt(self.state.get("cooldown_until"))
        if cooldown and self.started < cooldown:
            self.record["status"] = "skipped_cooldown"
            self.record["cooldown_until_utc"] = _iso(cooldown)
            log.warning("In 429 cool-down until %s: no API calls this run", cooldown)
            return self.finish(deadline=self.mono0 + NORMAL_DEADLINE_S)

        plan = self.plan()
        budget = FULL_DEADLINE_S if (plan["prices_full"] or plan["stations_full"]) \
            else NORMAL_DEADLINE_S
        deadline = self.mono0 + budget
        self.record.update({"deadline_s": budget,
                            "plan": {k: (_iso(v) if isinstance(v, datetime) else v)
                                     for k, v in plan.items()}})
        try:
            with hard_limit(budget + HARD_KILL_MARGIN_S):
                self.client = FuelFinderClient(
                    fuel_finder_settings(), token_cache=self.p.token,
                    deadline=deadline - UPLOAD_RESERVE_S)
                self.run_task("prices_incremental", PRICES_PATH,
                              {"effective-start-timestamp": _fmt_since(plan["prices_since"])},
                              kind="prices", full=False, complete=False)
                self.run_task("stations_incremental", STATIONS_PATH,
                              {"effective-start-timestamp": plan["stations_since"]},
                              kind="stations", full=False, complete=False)
                if plan["prices_full"]:
                    self.run_task("prices_full", PRICES_PATH, {}, kind="prices", full=True,
                                  complete=plan["prices_full_complete"])
                elif plan["stations_full"]:
                    self.run_task("stations_full", STATIONS_PATH, {}, kind="stations",
                                  full=True, complete=True)
                statuses = {t["status"] for t in self.record["tasks"]}
                self.record["status"] = ("ok" if statuses <= {"ok"} else
                                         "partial" if "ok" in statuses else "failed")
        except RateLimitedError as e:
            wait = max(COOLDOWN_S, e.retry_after_s or 0) + random.uniform(15, 60)  # noqa: S311
            until = datetime.now(UTC) + timedelta(seconds=wait)
            self.state["cooldown_until"] = _iso(until)
            self.record.update({"status": "rate_limited", "error": str(e),
                                "cooldown_until_utc": _iso(until)})
            log.error("HTTP 429: stopping; no API calls until %s", until)
        except DeadlineExceeded as e:
            self.record.update({"status": "deadline", "error": str(e)})
            log.warning("Deadline reached: %s", e)
        except HardTimeout as e:
            self.record.update({"status": "hard_timeout", "error": str(e)})
            log.error("%s", e)
        except GeoBlockedError as e:
            self.record.update({"status": "geo_blocked", "error": str(e)})
            log.error("%s", e)
        except Exception as e:  # never lose the run record
            self.record.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
            log.exception("Unexpected error")
        if self.client:
            self.record["api"] = {
                "requests": self.client.request_count, "retries": self.client.retry_count,
                "status_counts": self.client.status_counts,
                "tokens_generated": self.client.tokens.generate_count,
                "tokens_refreshed": self.client.tokens.refresh_count,
                "request_ids_sample": self.client.request_ids[:5],
            }
        self.record["join_check"] = self.join_check()
        return self.finish(deadline=deadline + UPLOAD_RESERVE_S)

    def finish(self, *, deadline: float) -> dict[str, Any]:
        from milemap.storage.r2 import R2ConfigError, r2_client, upload_dir

        self.state["last_run_started_at"] = _iso(self.started)
        self.state["last_run_status"] = self.record["status"]
        if self.record["status"] in ("ok", "partial"):
            self.state["last_ok_run_started_at"] = _iso(self.started)
        _save_json(self.p.state, self.state)
        for k, v in self.hashes.items():
            _save_json(self.p.hashes(k), v)

        upload: dict[str, Any] = {}
        try:
            client, bucket = r2_client()
            r = upload_dir(self.p.outbox, client=client, bucket=bucket, move_to=self.p.mirror,
                           deadline=deadline)
            upload = {"uploaded": r.uploaded, "already_present": r.already_present,
                      "remaining_in_outbox": r.remaining, "bytes": r.bytes,
                      "conflicts": r.conflicts, "errors": r.errors[:10]}
        except R2ConfigError as e:
            upload = {"error": str(e)}
        except Exception as e:
            upload = {"error": f"{type(e).__name__}"}
            log.warning("R2 upload step failed: %s", type(e).__name__)

        self.record["upload"] = upload
        self.record["finished_at_utc"] = _iso(datetime.now(UTC))
        self.record["seconds"] = round(time.monotonic() - self.mono0, 1)
        rel = (f"_runs/source={SOURCE}/date={self.started:%Y-%m-%d}/{self.run_id}.json")
        write_json_once(self.p.outbox, rel, self.record)
        with self.p.runs_log.open("a") as f:
            f.write(json.dumps({k: self.record.get(k) for k in (
                "run_id", "runner", "started_at_utc", "finished_at_utc", "seconds", "status",
                "seconds_since_previous_run", "gap", "error")} | {
                "tasks": [(t["task"], t["status"], t["records_returned"], t["rows_written"])
                          for t in self.record["tasks"]],
                "requests": self.record.get("api", {}).get("requests"),
                "upload": {k: upload.get(k) for k in ("uploaded", "remaining_in_outbox",
                                                      "error")},
            }, default=str) + "\n")
        # second, tiny upload pass so this run's record lands in R2 now (best effort)
        try:
            client, bucket = r2_client()
            upload_dir(self.p.outbox, client=client, bucket=bucket, move_to=self.p.mirror,
                       deadline=time.monotonic() + 10)
        except Exception:  # noqa: S110 - next run retries the outbox
            pass
        return self.record


def tick(runner: str | None = None, root: Path | None = None) -> dict[str, Any]:
    paths = Paths(root or data_dir())
    runner = runner or os.environ.get("MILEMAP_RUNNER", "manual")
    with run_lock(paths.lock) as acquired:
        if not acquired:
            log.warning("Another collector run is still in progress; skipping")
            return {"status": "skipped_locked"}
        return Tick(paths, runner).execute()


def gaps_report(days: int = 7, root: Path | None = None) -> dict[str, Any]:
    """Summarise coverage from the local run log and gap records."""
    paths = Paths(root or data_dir())
    since = datetime.now(UTC) - timedelta(days=days)
    runs = []
    if paths.runs_log.exists():
        for line in paths.runs_log.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if _dt(r.get("started_at_utc")) and _dt(r["started_at_utc"]) >= since:
                runs.append(r)
    gaps = []
    for base in (paths.mirror, paths.outbox):
        for g in (base / f"_gaps/source={SOURCE}").glob("*.json"):
            d = _load_json(g, {})
            if _dt(d.get("to_utc")) and _dt(d["to_utc"]) >= since:
                gaps.append(d)
    gaps.sort(key=lambda g: g["from_utc"])
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    covered = sum(min(r.get("seconds_since_previous_run") or INTERVAL_S, GAP_THRESHOLD_S)
                  for r in runs if r["status"] in ("ok", "partial"))
    return {
        "window_days": days, "runs": len(runs), "runs_by_status": by_status,
        "first_run_utc": runs[0]["started_at_utc"] if runs else None,
        "last_run_utc": runs[-1]["started_at_utc"] if runs else None,
        "gaps": [{k: g.get(k) for k in ("from_utc", "to_utc", "seconds", "cause")}
                 for g in gaps],
        "total_gap_hours": round(sum(g.get("seconds", 0) for g in gaps) / 3600, 2),
        "approx_covered_hours": round(covered / 3600, 2),
    }
