# Fuel Finder history depth (strategy step 1)

**Question:** if we ask the incremental endpoints for everything since launch
(`effective-start-timestamp=2026-02-02`), do we get a full change log, or only the latest
state? Checked for **prices** and for **station information** (PFS).

**Answer: only the latest price.** The API keeps no history that we can reach. Each station and
fuel appears exactly once, with its current price and the time that price took effect.
"Since 2026-02-02" returns exactly the same 8,142 stations and 26,528 prices as the
full download. **There is nothing to backfill: the UK archive starts with our own first
recording (4 November 2026, 22:15 UTC).** Recovering anything earlier now depends on
strategy step 2 (Wayback Machine snapshots).

**Station information is the same: latest version only**, and station records carry **no
timestamp at all**. Station history (openings, closures, brand changes, amenity changes) can
only come from our own daily snapshots. See [Station information](#station-information-pfs)
below.

| | |
|---|---|
| Runs | Prices 4 November 2026 22:15–22:18 UTC; stations 22:24–22:25 UTC; from a UK home connection (no VPN) |
| Commands | `uv run milemap probe history-depth` (`--quick` first) and `uv run milemap probe history-depth --part stations` |
| Requests | 101 (prices) + 58 (stations and join) requests, sequential, ≤ 1 request every 0.75 s; 0 retries |
| Tokens | 1 access token, reused for every request (never one per call) |
| Code | branch `feat/history-depth-probe`, `src/milemap/sources/fuel_finder/probe.py` |
| Raw evidence | every response saved write-once under `source=fuel_finder/` in the local raw store (git-ignored); one manifest per run |

## Results

| Request (`effective-start-timestamp`) | Batches | Stations | Price rows | Distinct effective times per station + fuel |
|---|---|---|---|---|
| *(none: full download)* | 17 (16 × 500 + 142, then 404) | 8,142 | 26,528 | always 1 |
| `2026-11-04 21:15:55` (last 1 h) | 1 | 68 | 241 | always 1 |
| `2026-11-04 16:15:55` (last 6 h) | 1 | 445 | 1,512 | always 1 |
| `2026-11-05` (**tomorrow, control**) | 1 (empty) | **0** | **0** | — |
| `2026-11-03` (1 day) | 7 | 3,227 | 11,022 | always 1 |
| `2026-10-28` (7 days) | 13 | 6,074 | 20,513 | always 1 |
| `2026-10-05` (30 days) | 16 | 7,588 (7,586 distinct) | 25,066 | always 1 |
| `2026-08-06` (90 days) | 17 | 8,106 | 26,445 | always 1 |
| **`2026-02-02` (since launch)** | 17 | **8,142** | **26,528** | **always 1** |

How to read this:
- **The filter works.** The control (a future date) returns nothing, and wider windows return
  steadily more stations.
- **It is not a change log.** In no window does any station + fuel pair have more than one
  effective timestamp, and the since-launch result equals the full snapshot exactly. A station
  that changed price ten times since February shows only its latest price.
- **The oldest current price** took effect on 2026-02-06. 138 prices (0.5 %) haven't changed in
  90+ days.

## Request format

| Item | Finding |
|---|---|
| Token | `POST {BASE}/api/v1/oauth/generate_access_token`, JSON body `{client_id, client_secret}`. Response `{success, message, data: {access_token, token_type: "Bearer", expires_in: 3600, refresh_token, refresh_token_expires_in: 172800}}`. Access token lasts 1 hour, refresh token 48 hours. Refresh via `/api/v1/oauth/regenerate_access_token` (implemented; not yet exercised because a full run takes minutes). |
| `effective-start-timestamp` formats | **All three accepted, with identical results:** `YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`, `YYYY-MM-DDTHH:MM:SSZ`. |
| Time precision | **Works to the second.** "Last 1 hour" returned 68 stations against 3,227 for "since yesterday". |
| Pagination | `batch-number` 1, 2, 3…, 500 stations per batch. Past the end the API returns **404** (not an empty list). |
| Speed | Full download: 17 requests in 33 s. A 1-hour incremental is a single request. |

## Response shape

One record per **station**, with a nested list of fuels:

```json
{
  "node_id": "<64-character hex id>",
  "trading_name": "…",
  "public_phone_number": "…",
  "fuel_prices": [
    {"fuel_type": "E10", "price": 139.9,
     "price_last_updated": "2026-11-04T22:14:30.000Z",
     "price_change_effective_timestamp": "2026-11-04T22:14:30.000Z"}
  ]
}
```

- No wrapper object around data responses (a bare JSON list), unlike the token response.
- Prices endpoint carries **no brand, address or coordinates**; those come from the stations
  endpoint (`/api/v1/pfs`), which is not probed yet.
- `public_phone_number` is blank for 631 stations.

## Station information (PFS)

Endpoints: `GET /api/v1/pfs?batch-number=N` (all stations) and the same path with
`effective-start-timestamp` (incremental).

| Request (`effective-start-timestamp`) | Batches | Stations | Versions per station |
|---|---|---|---|
| *(none: full download)* | 17 | 8,148 | always 1 |
| `2026-11-05` (**tomorrow, control**) | none (**404 on batch 1**) | **0** | — |
| `2026-11-04 16:24:22` (last 6 h) | none (404 on batch 1) | 0 | — |
| `2026-11-03` (1 day) | 1 | 6 | always 1 |
| `2026-10-28` (7 days) | 1 | 34 | always 1 |
| `2026-10-05` (30 days) | 1 | 130 | always 1 |
| `2026-08-06` (90 days) | 3 | 1,171 | always 1 |
| **`2026-02-02` (since launch)** | 9 | **4,366** | **always 1** |

How to read this:
- **The filter works, and it's not a change log.** Each station appears once, in its current
  form. Every window is a subset of the full list.
- **"Since launch" returns only 4,366 of 8,148 stations.** The other ~3,800 records haven't
  changed since before 2 February 2026, presumably registered during onboarding before the
  statutory start. So the incremental endpoint can never rebuild the full list: **a full
  download is required** to have every station.
- **Station records change rarely:** about 6 a day, 34 a week, 130 a month.
- **No timestamp field exists in station records.** The filter uses an internal "last updated"
  time that the API doesn't expose. We can only date a change to the run that first saw it.
- **When nothing matches, this endpoint returns 404 on batch 1,** whereas the prices endpoint
  returns an empty list. The client treats both as "no more data". The collector logs a batch-1
  404 explicitly so that a broken endpoint can't pass silently as "nothing changed": the daily
  full download would expose it.

Fields in every station record (counts of blank values in brackets):
`node_id` (64-char hex), `trading_name`, `brand_name` (9 blank), `is_same_trading_and_brand_name`,
`public_phone_number` (634 blank), `temporary_closure` (63 true), `permanent_closure`
(**null for 1,404**, otherwise false), `permanent_closure_date` (always null),
`is_motorway_service_station` (168 true), `is_supermarket_service_station` (2,332 true),
`location` {`address_line_1`, `address_line_2` (4,741 blank), `city` (250 blank), `county`
(4,586 blank), `country` (729 blank), `postcode`, `latitude`, `longitude`}, `amenities` (list;
210 empty), `opening_times` {`usual_days`, `bank_holiday`}, `fuel_types` (list; 1 empty).

Notes for staging: `permanent_closure` is three-valued (true/false/null) and must not be read as
false when null. No station is marked permanently closed at the moment. One latitude came back
as an integer. Country and county are often blank, so geography must come from coordinates or
postcode (ONS lookup), not these fields.

## Stations ↔ prices join (`node_id`)

Compared two full downloads taken 35 seconds apart (stations `run_20261104T222422Z_88600ddd`,
prices `run_20261104T222457Z_b49ab726`).

| | Count |
|---|---|
| Stations with a station record | 8,148 |
| Stations with prices | 8,142 |
| In both | 8,142 |
| **Prices but no station record** | **0** |
| **Station record but no prices** | **6** |

The 6 stations without prices: 5 are flagged `temporary_closure = true`, and 1 is not
flagged at all (its name suggests a motorway services site). 58 other temporarily closed stations *do* still have prices, so
"temporarily closed" and "has prices" are independent. The map must show closure status next to
any price rather than relying on missing prices. The join check runs on every collector run and
both lists are recorded, because the counts will change over time.

## Findings that change the design

1. **The incremental filter works per station, not per price.** When any fuel at a station
   changes, the API returns *all* that station's fuels, including ones unchanged for months.
   For example, in the "last 1 hour" window 132 of 241 rows took effect before the cut-off.
   Staging must therefore **detect real changes by comparing with the previous observation**,
   not treat every returned row as a change.
2. **Stations are sorted newest-updated first, and that order shifts while you page.** Batch 1
   covered today, batch 17 went back to February. If a station changes while we're paging, it
   jumps to batch 1, so later stations can shift across a batch boundary. One station can then
   appear twice, and another can be skipped. **This was observed:** the 30-day window returned
   2 stations twice (7 duplicate rows). The collector must deduplicate, and a regular full
   download must catch anything skipped.
3. **Prices arrive late, and the effective time can be earlier than the submission time.**
   Comparing `price_last_updated` (submission) with `price_change_effective_timestamp`:

   | Submission minus effective time | Prices |
   |---|---|
   | 0 (identical) | 16,923 |
   | under 1 minute | 3,791 |
   | 1–30 minutes | 5,464 |
   | 30 minutes to 24 hours | 55 |
   | **over 24 hours (up to ~10.7 days)** | **26** |
   | **negative (effective *after* submission, as much as about 1 hour)** | **269** |

   So a price change can surface long after the moment it "happened". The poller's lookback
   window must overlap generously, and staging must key events on the **effective** time (as
   planned) while keeping the submission time. The negative gaps of about 1 hour look like
   time-zone mistakes by some submitters (local time recorded as UTC) or scheduled future
   changes. We flag them and don't "fix" them.
4. **Fuel codes are upper case:** `E10`, `E5`, `B7_STANDARD`, `B7_PREMIUM`, `B10`, `HVO`. The
   field guide writes `B7_Standard`/`B7_Premium`, so the mapping must be case-insensitive. Current
   counts: B7_STANDARD 8,045 · E10 7,927 · E5 6,267 · B7_PREMIUM 4,171 · B10 62 · HVO 56.
5. **Prices are pence per litre, JSON numbers** (230 sent as integers, e.g. `150`). The observed
   range is exactly 100.9–299.9p, with **no values outside 100–300p**. The service may already
   reject out-of-range submissions. Our range flag stays as a safety net.
6. **Timestamps are UTC with a `Z` suffix and millisecond precision**, as documented.

## Recommended collector settings (replacing the plan's assumptions)

| Job | What | Cost |
|---|---|---|
| Every 5 minutes | Incremental, `effective-start-timestamp` = now − **2 hours** (overlapping windows; deduplicated on write) | about 1–2 requests |
| Every hour | Full download (catches stations skipped by shifting pages and late submissions) | 17 requests, about 35 s |
| Every 5 minutes | Station information incremental, since the start of the previous day (overlapping; usually 1 request) | 1 request |
| Daily | Station information full download (the only way to get all 8,148 stations) | 17 requests |

That comes to roughly 1,150 requests a day, about 0.8 requests a minute on average and far below
the limit.

**Storage:** a full snapshot is about 1 MB of Parquet. Saving every hourly snapshot in full
would take about 9 GB a year, which is close to the R2 free tier. The collector therefore
saves only **station records whose content changed** since the last stored observation (by
record hash), plus one complete snapshot a day (about 0.4 GB a year). Expected total: well under
1 GB a year for prices. Stations add about 1.4 MB per daily full snapshot (about 0.5 GB a year), plus a few changed records from each incremental poll.

## What this means for MileMap

- **No backfill job.** Step 5 of the plan (poller) proceeds without one.
- **Every day without the poller is permanently lost history**, so the poller is now the top
  priority.
- **Strategy step 2 (Wayback Machine) becomes more valuable.** It is the only possible source
  of UK station history before 4 November 2026.
- History-based UK signals stay labelled "collecting data" from 4 November 2026. The Germany
  Lab carries the market-behaviour analysis until then.

## Not yet tested

- Refresh-token flow in a live run (unit-tested only).
- Whether the filter compares against the effective time, the submission time or an internal
  station "last updated" time. The per-station behaviour suggests the latter. The overlap
  window and hourly full download make the collector safe either way.
