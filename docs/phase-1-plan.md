# Phase 1 plan: data foundation

**Status:** draft, waiting for approval. No code is written until this is approved.
**Goal:** start the UK archive clock as soon as possible. Every UK price change gets recorded from the day the collector goes live, and the other Phase 1 sources and the dbt staging layer are built around it.

---

## 0. What we know (and don't) about the Fuel Finder API

Everything below must be checked against the official developer portal (`https://www.developer.fuel-finder.service.gov.uk`, behind GOV.UK One Login) before the probe is written. The portal is the source of truth; the other sources are third-party.

| Item | Current understanding | Source | Confidence |
|---|---|---|---|
| Auth | OAuth 2.0 client credentials; `POST /api/v1/oauth/generate_access_token`; token lasts ~1 h; a refresh endpoint exists | GOV.UK guidance; dev.to write-up | Medium. Path and body format unconfirmed |
| Stations | `GET /api/v1/pfs?batch-number=N`, 500 per batch | dev.to; `ukfuelfinder` lib | Medium |
| Prices | `GET /api/v1/pfs/fuel-prices?batch-number=N` | dev.to | Medium |
| Incremental | A "changed since" mode exists for prices and stations; the library passes a `YYYY-MM-DD` date | `ukfuelfinder` lib | **Low.** HTTP path and param name unknown |
| Pagination | No total count. Stop on an empty or short batch (<500); invalid batch numbers return 404 | dev.to; lib changelog | Medium |
| Keys / fields | `node_id` (station), `fuel_type`, price, `price_change_effective_timestamp`, `price_last_updated` (= last *change*, not last confirmation) | lib; dev.to | Medium |
| Removed fields | `mft_organisation_name` removed Feb 2026; `success`/`message` removed | lib changelog | High |
| Rate limit | ~100 requests/min, **one request at a time** | dev.to (quoting the docs) | Medium |
| Geo | 403 from non-UK IPs | dev.to; CLAUDE.md | High |
| Data quirks | Duplicate rows; country field inconsistent; postcodes missing the space; some lat/lon swapped or sign-flipped; `00:00–00:00` + `is_24_hours=false` means "not filled in" | dev.to | Medium |
| Public CSV | Updated twice daily, also offered by email link; **URL not yet known** | GOV.UK guidance | Must find |

Also unknown: whether timestamps carry a UTC offset or are naive London local time; whether prices are pence or pounds; the exact fuel-type codes (E10/E5/B7/…?). The probe answers all three.

---

## 1. Folder structure

```
milemap/
├── CLAUDE.md
├── README.md                     # what, how to run, freshness badge, attribution
├── LICENSE                       # MIT (code)
├── .env.example                  # credential template (real .env is git-ignored)
├── .gitignore
├── pyproject.toml                # uv project, Python 3.12 (uv-managed; system Python is 3.9)
├── uv.lock
├── src/milemap/
│   ├── config.py                 # pydantic-settings: reads env / .env, fails fast if missing
│   ├── http.py                   # shared httpx client: timeouts, retry/backoff on 429/5xx, UA "MileMap/<ver>"
│   ├── storage/
│   │   ├── raw.py                # append-only Parquet writer (write-once, never overwrite)
│   │   ├── manifest.py           # per-run manifest (counts, hashes, validation result)
│   │   └── state.py              # watermarks (last successful incremental "since")
│   ├── sources/
│   │   ├── fuel_finder/
│   │   │   ├── auth.py           # token fetch + cache + early refresh
│   │   │   ├── client.py         # paged GETs, sequential, rate-limited, 403 → GeoBlockedError
│   │   │   ├── collect.py        # jobs: prices_incremental, prices_full, stations_full
│   │   │   └── probe.py          # history-depth test (one-off)
│   │   ├── fuel_finder_csv.py    # public CSV fallback / bootstrap
│   │   ├── desnz_weekly.py       # DESNZ weekly road fuel prices (2003 →)
│   │   ├── brent.py              # Brent daily (FRED DCOILBRENTEU)
│   │   └── tankerkoenig.py       # German history for the Lab
│   ├── validate.py               # per-run checks; result goes into the manifest
│   └── cli.py                    # `uv run milemap collect fuel-finder prices --mode incremental`, etc.
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml              # DuckDB; paths from env vars (no secrets)
│   ├── packages.yml              # dbt_utils, dbt_expectations
│   ├── models/staging/
│   │   ├── fuel_finder/          # _sources.yml, _models.yml, stg_*.sql
│   │   ├── fuel_finder_csv/
│   │   ├── desnz/
│   │   ├── fred/
│   │   └── tankerkoenig/
│   ├── macros/                   # to_utc(), normalise_postcode(), price_in_range()
│   ├── seeds/                    # fuel_type_map.csv, brand_operator_map.csv (stub), uk_bbox
│   └── tests/                    # singular tests (DST, coordinate sanity)
├── tests/                        # pytest
│   ├── fixtures/                 # recorded, anonymised API responses (small)
│   └── test_*.py
├── deploy/collector/             # Dockerfile + schedule config for the UK host
├── data/                         # local raw store in dev (git-ignored)
├── docs/
│   ├── phase-1-plan.md           # this file
│   ├── history-depth.md          # probe results (strategy step 1)
│   ├── raw-storage.md            # layout + immutability guarantees
│   ├── data-sources.md           # every source: URL, licence, cadence, quirks
│   └── compliance.md             # Fuel Finder terms, checked again before launch
└── .github/workflows/ci.yml
```

Python libraries: `httpx`, `tenacity`, `pyarrow`, `duckdb`, `fsspec` + `s3fs` (R2 and Azure both speak S3 or have fsspec drivers), `pydantic-settings`, `typer`, `structlog`. Dev: `pytest`, `respx` (HTTP mocking), `ruff`. dbt: `dbt-duckdb` ≥ 1.8 (needed for unit tests).

---

## 2. First task: the history-depth test (strategy step 1)

**Goal:** find out exactly what `incremental prices since 2026-02-02` returns: the latest price per station only, or a full change log. Record the result in `docs/history-depth.md`.

**Before the probe:** you log in to the developer portal and give me the exact incremental endpoint path, parameter name and date format, either by pasting from the docs or by saving the OpenAPI spec into `docs/`. Then I write the probe against the real spec, not guesses.

**Where it runs:** from your Mac on your home connection (a UK IP), with `.env` filled in. No server is needed for the probe.

**What the probe does** (`uv run milemap probe history-depth`):
1. Gets a token. It records the response *shape* (field names, `expires_in`) and never the token itself.
2. Calls **full prices**, all batches, as the baseline. It records rows, distinct `(node_id, fuel_type)` pairs and the field list.
3. Calls **incremental since 2026-02-02**, all batches. It records:
   - total rows vs distinct `(node_id, fuel_type)` pairs (rows > pairs means a change log);
   - a histogram of rows per pair;
   - min/max of `price_change_effective_timestamp`, and whether any pair has several distinct effective timestamps;
   - for 5 sample stations, the full sequence returned.
4. Calls incremental with other "since" values (yesterday, −7 d, −30 d, −90 d) and compares the row counts.
5. Tests granularity: does the param accept a datetime (`2026-11-04T10:00:00Z`) or only a date? This decides how tight the 5-minute polling loop can be.
6. Inspects formats: timestamp offset (UTC or naive), price unit (pence or pounds), the fuel-type code list, and nulls in the key fields.
7. Saves every response through the real raw writer (§3) so the probe data becomes the first archive entries, and writes a JSON summary that `docs/history-depth.md` is written from.

It runs sequentially at ≤ 1 request per second, well under the stated limit.

**Decision after the probe:**
- **Change log →** add a `backfill` job that pages the whole history once into `raw/…/endpoint=prices_backfill/`, before anything else.
- **Latest only →** document that; the archive really starts at collector go-live, and strategy step 2 (Wayback) matters more.

---

## 3. Raw storage layout

**Principles:** append-only; write-once (a writer that finds an existing key raises an error and does not overwrite); every file traceable to the run that produced it; payloads kept verbatim so later API schema changes never lose information.

```
{MILEMAP_RAW_ROOT}/
  source=fuel_finder/
    endpoint=prices_incremental/ingest_date=2026-11-04/run_20261104T201500Z_a1b2c3d4.parquet
    endpoint=prices_full/        ingest_date=…/run_….parquet
    endpoint=stations_full/      ingest_date=…/run_….parquet
    endpoint=prices_backfill/    …                         (only if the probe finds a change log)
  source=fuel_finder_csv/   endpoint=current/        ingest_date=…/run_….parquet   (+ original .csv.gz alongside)
  source=desnz/             endpoint=weekly/         ingest_date=…/run_….parquet   (+ original file)
  source=fred/              endpoint=brent_daily/    ingest_date=…/run_….parquet
  source=tankerkoenig/      endpoint=prices|stations/ data_date=…/run_….parquet
  _manifests/source=…/run_….json
  _state/fuel_finder_prices_incremental.json        # the only mutable object: watermark
```

**Parquet row schema (all API sources):**

| column | type | notes |
|---|---|---|
| `_run_id` | string | `run_<UTC ts>_<8-char uuid>` |
| `_fetched_at_utc` | timestamp | when the HTTP response arrived |
| `_request_url` | string | without credentials |
| `_request_params` | string (JSON) | e.g. since, batch-number |
| `_batch_number` | int | |
| `_record_index` | int | position in the batch |
| `_record_hash` | string | sha256 of the record JSON (for dedupe audits) |
| `record_json` | string | **the verbatim record** |

File-based sources (CSV, DESNZ, Tankerkönig) keep the original file next to the Parquet, plus typed columns as strings so nothing is coerced at ingest.

**Manifest** (`_manifests/…json`): run id, source, endpoint, params, start/end, HTTP status per batch, row count, file sha256, `validation: {passed, checks[]}`, code version (git sha).

**Validation (gate):** raw is *always* written, because losing data is worse than storing bad data, but the watermark only advances and the run only counts as "good" when checks pass:
- the response parsed and the required keys are present (`node_id`, `fuel_type`, price, effective timestamp);
- full-run row count is within ±10 % of the last good full run;
- ≥ 95 % of prices fall within 100–300p;
- no batch errors were left unhandled.
In Phase 2 the "publish current prices" step reads only from runs marked good, which keeps the previous good data live.

**Storage backend:** local `./data/raw` in development, and **Cloudflare R2** in production (decided; see §3a). The code only sees an fsspec URL.

**Write-time dedupe (protects the free storage budget):** if the incremental endpoint only accepts a *date* (not a datetime), each 5-minute poll re-returns every change since midnight. That's mostly the same records again and again. The collector therefore writes only records whose `_record_hash` it hasn't already stored for that "since" window. The manifest still records the full count returned, so the raw store stays a faithful log of every *distinct* observation without saving 288 copies a day.

**Collector schedule (production):**
- `prices_incremental` every 5 min, with a "since" that overlaps the last good watermark (exact overlap depends on probe step 5); duplicates are fine because staging dedupes;
- `prices_full` daily as reconciliation (catches anything the incremental feed missed);
- `stations_full` daily;
- requests are sequential, with backoff on 429/5xx and an explicit `GeoBlockedError` on 403 so it isn't mistaken for an auth failure.

---

## 3a. Free hosting design (decided: £0/month target)

### Collector: Azure Container Apps **Job** in UK South (consumption plan)
Unlike the Azure free-account VM, which is only free for 12 months, the Container Apps consumption plan has a **monthly free grant that doesn't expire**: 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 M requests per subscription. Jobs have no idle or request charges.

| Job | Schedule | Size | Budget per month (worst case) |
|---|---|---|---|
| `prices-incremental` | cron `*/5 * * * *` | 0.25 vCPU / 0.5 GiB, timeout 60 s | 8,640 runs × 60 s → 129,600 vCPU-s, 259,200 GiB-s |
| `prices-full` + `stations-full` | daily | same, timeout 10 min | ≤ 30 × 600 s → 4,500 vCPU-s, 9,000 GiB-s |
| **Total (worst case)** | | | **~134k / 180k vCPU-s, ~268k / 360k GiB-s** — inside the grant |

Typical runs should take 10–20 s, so real usage would be about a third of this.

Hidden-cost traps and how we avoid each:
- **Container registry:** Azure Container Registry Basic costs about $5/month, so we use **GitHub Container Registry (ghcr.io)** instead. Public images there are free, and the repo is public anyway.
- **Log Analytics:** set the environment's logs destination to *none*; run logs go into our own manifests in R2.
- **No VM, no public IP, no load balancer, no Azure Storage account:** state and raw files live in R2.
- **Outbound bandwidth:** Azure's first 100 GB/month is free, and uploads to R2 are a few MB per day.
- **Budget guardrail:** an Azure **budget alert at £1/month**. Azure free and pay-as-you-go accounts ask for a card for identity checks; the alert emails you before anything meaningful could be charged. (Pay-as-you-go has no hard spending cap, so the alert is the safety net.)

**Risk:** Fuel Finder might block cloud data-centre IPs as well as non-UK ones. Mitigation: the very first thing deployed is the history-depth probe running *from the Azure job*, before we build on that assumption. Fallbacks, both free: (a) an Oracle Cloud "Always Free" VM in its London region; (b) a scheduled job on your Mac (has gaps when it sleeps).

### Raw store: Cloudflare R2 (free tier)
Free each month: **10 GB-month storage**, 1 M Class A (write) operations, 10 M Class B (read) operations, **no egress fees**. It's also where the Phase 2 website reads from.

| Item | Estimate | Notes |
|---|---|---|
| Fuel Finder prices (incremental + daily full, Parquet zstd, deduped) | ~0.5–1.5 GB / year | ~8,400 stations × ~4 fuels |
| Fuel Finder stations (daily full) | ~0.5–1 GB / year | could drop to "only when changed" if needed |
| CSV fallback, DESNZ, Brent | < 0.2 GB / year | small |
| Writes | ~30–50 k Class A / month | 3% of the free 1 M |
| **Total UK raw** | **~1.5–3 GB / year** | **10 GB lasts roughly 3+ years** |

- **Tankerkönig is not stored in R2.** The full German archive is many GB and can be re-downloaded at any time. It stays in a local cache on your Mac (git-ignored), and only small derived Lab outputs go to R2 later.
- **Long-term overflow, still free:** the open dataset is published to a **Hugging Face dataset repo**, which is free for public datasets and has much more room than R2's free tier, plus GitHub Releases. Once a month is closed, its raw files are mirrored to Hugging Face, which doubles as an off-site immutable backup. If R2 ever nears 10 GB, older months can be served from Hugging Face. This is a later decision, and nothing is deleted without asking you.
- Other free tiers compared: Backblaze B2 (10 GB, egress limits), Azure Blob (5 GB, 12 months only), Oracle (20 GB, but a separate account). R2 is the best fit because of zero egress and the Phase 2 website.

---

## 4. Other ingestion modules

| Module | Fetch | Cadence | Notes |
|---|---|---|---|
| `fuel_finder_csv` | The public CSV (URL to be found on GOV.UK or in the subscription email) | Twice daily | Bootstrap and fallback; works without credentials. If no stable URL exists, document it and skip. |
| `desnz_weekly` | DESNZ "Weekly road fuel prices" CSV (data.gov.uk / GOV.UK) | Weekly | Full file each time (small); dedupe in staging. OGL v3.0. |
| `brent` | FRED `DCOILBRENTEU` (keyless CSV endpoint; API key optional) | Daily | USD/bbl; note the EIA origin in attribution. |
| `tankerkoenig` | Daily price/station CSVs from the Tankerkönig data repo, downloading **selected dates** over HTTPS rather than cloning the whole multi-GB repo | Initial: a bounded window (e.g. last 24 months); then daily | CC BY-NC-SA 4.0. Attribution recorded in `docs/data-sources.md`. Repo URL and licence to be re-checked before writing. |

All modules share the same pattern: `fetch → write raw (write-once) → manifest → validate`.

---

## 5. dbt staging models and tests

Sources are declared over the raw Parquet via `read_parquet('…/**/*.parquet', hive_partitioning=true)`. Freshness is configured on `_fetched_at_utc`.

| Model | Grain / key | Key logic |
|---|---|---|
| `stg_fuel_finder__prices` | one row per `(node_id, fuel_type, price_effective_at_utc)` | parse `record_json`; price → pence (numeric); timestamps → UTC; map fuel codes through `seeds/fuel_type_map`; dedupe on the key, keeping the earliest `_fetched_at_utc` (first observation) and also `last_seen_at_utc`; `is_price_in_valid_range` flag (100–300p), never dropping rows |
| `stg_fuel_finder__stations` | one row per `node_id` per stations run (latest-per-day dedupe) | normalise postcode (upper case, single space); normalise country; detect swapped or sign-flipped lat/lon (flag + corrected columns); `opening_hours_unknown` for the 00:00–00:00 case; keep `brand_raw`, `is_supermarket_raw` as reported (not trusted); tolerate missing columns (`json_extract` returns NULL) |
| `stg_fuel_finder_csv__prices` / `__stations` | as above | same conventions, so they can be unioned with the API data later |
| `stg_desnz__weekly_prices` | one row per `week_date` | pence/litre for ULSP (petrol) and ULSD (diesel), plus duty/VAT columns if present |
| `stg_fred__brent_daily` | one row per `date` | drop "." missing markers; USD/bbl |
| `stg_tankerkoenig__prices` | one row per `(station_uuid, fuel, changed_at_utc)` | source is Europe/Berlin local time → UTC; e5/e10/diesel; € → cents |
| `stg_tankerkoenig__stations` | one row per `station_uuid` | |

**Tests (written alongside each model):**
- `unique` / `dbt_utils.unique_combination_of_columns` on every grain;
- `not_null` on keys and timestamps;
- `accepted_values` for `fuel_type` (after mapping), so an unmapped new code fails loudly;
- range: a `dbt_expectations` row-level check that the flag matches the 100–300p rule, plus a *warn* test if more than 2 % of a day's prices are out of range;
- coordinates: corrected lat/lon falls inside the UK bounding box;
- postcode matches the UK regex after normalisation;
- **DST:** dbt unit tests feed `to_utc()` the 2026-03-29 and 2026-10-25 transitions (UK and DE), including the ambiguous 01:00–02:00 hour;
- source freshness: Fuel Finder warns after 30 min and errors after 2 h; DESNZ after 10 days; FRED after 4 days.

Phase 1 stops at staging. Intermediate, marts and snapshots start in Phase 2.

---

## 6. CI (GitHub Actions)

On every PR: `ruff check` + `ruff format --check`, `pytest` (recorded fixtures only, never live Fuel Finder calls, since US runners would get 403 anyway), and `dbt build` against a small fixture raw store committed in `tests/fixtures/raw/`. A scheduled workflow (daily) for the sources that don't need a UK IP (DESNZ, FRED, Tankerkönig) can come later. The Fuel Finder collector never runs in Actions.

---

## 7. Build order (one branch/PR each)

| # | Branch | Delivers | Depends on |
|---|---|---|---|
| 1 | `chore/scaffold` | uv project, config, http client, CLI skeleton, ruff, minimal CI, README stub, MIT LICENSE | — |
| 2 | `feat/raw-storage` | write-once writer, manifest, state, tests (incl. "refuses overwrite") | 1 |
| 3 | `feat/fuel-finder-client` | auth + paged client + error types, tested with mocked responses | 1 |
| 4 | **`feat/history-depth-probe`** | probe + **run it on your Mac** + `docs/history-depth.md` | 2, 3, your credentials, endpoint details from the portal |
| 5 | `feat/fuel-finder-collector` | incremental/full/stations jobs, validation gate, (backfill if the probe says so) | 4 |
| 6 | `deploy/collector-uk` | ghcr.io image, Azure Container Apps Jobs (UK South), R2 bucket, £1 budget alert, re-run probe from Azure → **archive clock starts** | 5, your Azure + Cloudflare accounts |
| 7 | `feat/dbt-staging-fuel-finder` | dbt project, Fuel Finder staging models + tests, fixture raw store, `dbt build` in CI | 5 |
| 8 | `feat/fuel-finder-csv` | CSV fallback + staging | 2 |
| 9 | `feat/desnz-brent` | DESNZ weekly + Brent ingestion + staging | 2 |
| 10 | `feat/tankerkoenig` | German ingestion + staging (DE DST tests) | 2 |
| 11 | `docs/phase-1-wrap` | `data-sources.md`, `compliance.md` draft, README freshness badge, Phase 1 definition-of-done check | all |

Steps 1–6 are the critical path to starting the archive, and steps 8–10 can be done while the collector runs. If hosting approval takes a while, step 6 can begin as a stopgap scheduled job on your Mac (UK IP). It's flagged as interim because laptop sleep leaves gaps.

---

## 8. Open questions for you

Decided:
- ~~Collector host~~: Azure UK South, **free only**, as a Container Apps Job within the monthly free grant (§3a).
- ~~Raw storage~~: Cloudflare R2 free tier, with Hugging Face as free overflow and backup (§3a).
- ~~GitHub repo~~: `jasonzamp/milemap`, public; the user is the sole contributor (no co-author trailers).
- ~~Location~~: stays in `~/Desktop/milemap`.

Still open:
1. **Accounts you'll need to create** (all free; I can't do these for you): an Azure account (asks for a card for verification), a Cloudflare account with R2 enabled (R2 also asks for a payment method on file, though usage stays within the free tier), and later a Hugging Face account.
2. **Endpoint details.** Once you have portal access, paste the incremental endpoint spec (path, param name, date format), or save the OpenAPI/Swagger file into `docs/`.

## Sources consulted for §0
- GOV.UK: [Access the latest fuel prices and forecourt data via API or email](https://www.gov.uk/guidance/access-the-latest-fuel-prices-and-forecourt-data-via-api-or-email)
- dev.to: [Ingesting the UK Fuel Finder API: the rules that aren't in the docs](https://dev.to/fuelpricetoday/ingesting-the-uk-fuel-finder-api-the-rules-that-arent-in-the-docs-jc8) (third-party)
- PyPI: [ukfuelfinder](https://pypi.org/project/ukfuelfinder/) (unofficial client library; changelog)
