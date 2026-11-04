# Raw storage and the collector

The raw store is MileMap's archive: every observation from Fuel Finder, exactly as received,
**append-only and never overwritten**. Everything downstream (dbt, the map, the open dataset) can
be rebuilt from it.

## Where it lives

| Place | What | Notes |
|---|---|---|
| **Cloudflare R2**, bucket `milemap-raw` (private) | The archive | Writes use `If-None-Match: *`, so **R2 itself refuses to overwrite** an existing object. |
| `data/outbox/` (local) | Files waiting to upload | Written first, so nothing is lost if the network or R2 is down. Retried on every run. |
| `data/raw/` (local) | Mirror of everything uploaded | Files move here after a successful upload. |
| `data/state/` (local, mutable) | Collector state, token cache, dedupe hashes, run log | The only mutable files. Losing them is harmless: the next run re-downloads and may store some duplicates. |

All of `data/` is git-ignored.

## Layout (object keys)

```
source=fuel_finder/endpoint={endpoint}/ingest_date=YYYY-MM-DD/{run_id}_partNNNN.parquet
_manifests/source=fuel_finder/endpoint={endpoint}/{run_id}.json
_runs/source=fuel_finder/date=YYYY-MM-DD/{run_id}.json        ← one per collector run, always
_gaps/source=fuel_finder/gap_{fromUTC}_{toUTC}.json            ← whenever no run happened for > 10 min
```

Endpoints: `prices_incremental`, `prices_full`, `stations_incremental`, `stations_full`
(collector), plus `probe_format_tests` (history-depth test).

`run_id` = `run_{UTC start time}_{8 hex chars}`, e.g. `run_20261104T223631Z_3062852d`.

## Parquet columns

| Column | Meaning |
|---|---|
| `_run_id` | The run that fetched it |
| `_fetched_at_utc` | When the HTTP response arrived |
| `_request_path`, `_request_params` | Exactly what was asked (no credentials) |
| `_batch_number`, `_record_index` | Position in the response |
| `_record_hash` | SHA-256 of `record_json` |
| `record_json` | **The record exactly as received** (one station, with nested `fuel_prices` for prices) |

## What gets written: dedupe without losing information

The API returns each station's *current* state, and polls overlap on purpose, so most records
returned are identical to what we already have. The collector stores a station record **only when
its content differs from the last stored version** for that `node_id`. The manifest always records
`records_returned`, `rows_written` and `skipped_unchanged`, so nothing is silently dropped.

**Exception: complete snapshots.** The first full prices download of each UTC day, and every
daily full stations download, store **every** record. Each day therefore has a self-contained
snapshot that doesn't depend on earlier files.

## Collector schedule (every 5 minutes)

| Task | When | Lookback |
|---|---|---|
| `prices_incremental` | every run | now − 2 h; after a gap, back to 15 min before the last good poll (max 24 h) |
| `stations_incremental` | every run | since the start of the previous UTC day |
| `prices_full` | at most once every 55 min (hourly) | — |
| `stations_full` | once per UTC day, never in the same run as `prices_full` | — |

**Time limits:** each run has a deadline of **45 s**, or **120 s** if it includes a full download,
with a SIGALRM backstop 10 s later. A lock file stops runs overlapping. Measured on 4 Nov 2026: a
normal run takes about 4 s (4 requests), and a run with a full download about 18 s (22 requests).

**HTTP 429:** the run stops immediately, with no retries. A cool-down of at least 5 minutes
(plus jitter, longer if `Retry-After` says so) is saved, and runs during the cool-down make **no
API calls** and record `skipped_cooldown`. See [compliance.md](compliance.md).

**Validation:** each task's manifest has a `validation` block: required fields present; at least
95 % of prices within 100–300p; full downloads non-empty and within ±10 % of the previous full
count. A failure marks the task `suspect`, and the data is **still stored** (raw is never thrown
away). Downstream publishing will only use runs that passed.

## Coverage and gaps

Every run writes a **run record** (`_runs/…`), including runs that were skipped, rate-limited,
failed or timed out. It holds the status, the task summaries, request counts, the time since the
previous run and the `node_id` join check.

If more than **10 minutes** (two intervals) have passed since the previous run started, the run
writes a **gap record** (`_gaps/…`):

```json
{"kind": "no_run", "from_utc": "…", "to_utc": "…", "seconds": 18000,
 "missed_runs_estimate": 59, "cause": "mac_asleep",
 "sleep_wake_events": [{"at_utc": "…", "event": "Sleep"}, …],
 "recovery": "next poll looks back to 15 min before the gap (max 24 h); …"}
```

`cause` comes from the Mac's own power log (`pmset -g log`): `mac_asleep` if a Sleep event falls
inside the gap, `no_run_mac_awake_or_unknown` otherwise (e.g. logged out, collector stopped), or
`no_run_unknown` where the power log isn't available (Azure).

**What a gap costs:** the first poll after waking reaches back to before the gap, so **every
station's latest price is recovered**. Lost are only the price changes that were *superseded*
during the gap (a station that changed twice while the Mac slept: we see the second change only).
Gap records make that window explicit so analyses can exclude or flag it.

Check coverage locally:

```bash
uv run milemap collect gaps --days 7
```

## The Mac collector (interim, until Azure)

```bash
sh deploy/mac/install.sh     # install/reinstall the launchd agent (runs every 5 min while logged in)
sh deploy/mac/uninstall.sh   # stop it (data and state kept)
tail -f data/logs/collector.log
launchctl print gui/$(id -u)/uk.milemap.collector | grep -E 'state|last exit'
```

launchd runs the agent only while you're logged in and the Mac is awake. When the Mac sleeps,
missed runs are **not** replayed one by one: one run happens on wake, and it records the gap.
To keep gaps short, set the Mac to stay awake while on mains power (System Settings → Battery →
Options → "Prevent automatic sleeping on power adapter when the display is off").

## Not overwritten, ever

- Parquet files, manifests, run records and gap records are created with an atomic
  hard-link (`os.link`), which fails if the file already exists.
- Uploads use `If-None-Match: *`. If R2 already holds an object with the same name and the
  **same SHA-256**, it counts as already uploaded. If the content **differs**, it's reported as a
  conflict, left in the outbox and **never overwritten**.
- Tests cover both (`tests/test_raw_storage.py`, `tests/test_r2_upload.py`).
