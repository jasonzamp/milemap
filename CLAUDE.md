# MileMap — the price of a mile, wherever you live

## What this project is
MileMap is a free public website built around one zoomable map of the whole UK, designed to be used by millions of people. It shows:
1. **The price of a mile** — what it costs to drive one mile in a petrol car, a diesel car, an EV charged at home, and an EV charged at a public charger, down to street level.
2. **How local fuel markets behave** — which petrol stations set prices locally and which follow, lockstep pricing, price cycles, "rockets and feathers" (prices rising faster than they fall), and how much competition each area has.
3. **Who pays the most** — city vs rural, people with vs without driveways, and minutes of work needed to drive 100 miles.
4. **The UK's first open fuel price archive** — MileMap records every UK station price change from launch and publishes the full history as a free, openly licensed dataset (the UK equivalent of Germany's Tankerkönig archive).

It is also a portfolio project: it must demonstrate live API ingestion, SQL data cleaning with tests, automated pipelines, careful analysis with honest methodology, a production-grade public website, and a polished, highly interactive front end.

Audience: UK drivers (urban and rural), journalists, researchers, and employers reviewing the repo.

## Product principles
- **One zoomable map is the whole product.** No separate dashboard pages. Everything opens as panels/overlays on the map.
- **Local first.** Must be genuinely useful to someone in North London AND someone in a remote rural village.
- **Built for millions.** Static, CDN-served, no per-visitor server work. A traffic spike (e.g. press coverage) must cost almost nothing and never take the site down.
- **Neutral framing.** Report "price leadership" and "coordination signals", never accuse named businesses of collusion or call anything a "cartel". Every analytical claim links to the methodology.
- **Privacy.** No accounts, no tracking cookies. Postcodes are resolved in the browser and never stored. Analytics, if any, must be cookieless and privacy-friendly.
- **Open.** Code MIT-licensed; the price archive published openly with correct source attribution.

## Data sources
| Source | Content | Refresh | Notes |
|---|---|---|---|
| Fuel Finder API (UK gov, statutory since 2 Feb 2026) | Every UK forecourt: details, amenities, petrol/diesel prices with `price_change_effective_timestamp` | Stations report within 30 min of a change | OAuth 2.0 client credentials via GOV.UK One Login. Has full and incremental ("changed since date") endpoints, batches of 500. **Only answers UK IP addresses (403 otherwise).** Open Government Licence v3.0. |
| Fuel Finder public CSV | Current prices + forecourt details | Twice daily | No API needed. Good fallback / bootstrap. |
| DESNZ weekly road fuel prices (data.gov.uk) | National average petrol & diesel, June 2003 → present | Weekly | Long-run UK history for national rockets-and-feathers. |
| Brent crude (e.g. FRED) | Wholesale oil price | Daily | |
| Tankerkönig (Germany) | Every price change at ~15,000 German stations since 2014, daily CSVs in a git repo | Daily | Historical archive listed as CC BY-NC-SA 4.0 (live API CC BY 4.0) — non-commercial use, attribute properly. Used for the "Lab". |
| NSW FuelCheck (Australia) — optional | Monthly station-level price history files | Monthly | Optional third comparison country. |
| Octopus Energy public API | Agile half-hourly unit rates by region, past and next-day | Next-day prices ~16:00 daily | Request past periods for history. Output switches UTC/BST — handle carefully. |
| Charge point operator OCPI open data (Gridserve, GeniePoint, etc.) | Charger locations, connectors, tariffs, availability | Live | Required by Public Charge Point Regulations 2023. Formats vary — start with a few operators. |
| Ofgem price cap | Standard unit rate + standing charge by region | Quarterly | Seed/reference table. |
| postcodes.io | Postcode → lat/lon, area codes | On demand | Free, keyless. For bulk use, load the ONS postcode directory instead of hitting the API per visitor. |
| ONS rural–urban classification | Area type tags | Static | |
| Census 2021 (housing type, car availability) | Driveway-access proxy by area | Static | |
| DfT plug-in vehicle stats by postcode district | EV adoption | Quarterly | |
| ONS earnings (ASHE) by local authority | Local wages | Annual | For "minutes of work". |
| OpenRouteService / OSRM | Drive-time isochrones | Pre-computed | Defines each station's local market (10-min drive). |
| OpenStreetMap (amenity=fuel) | Station amenities enrichment | Periodic | ODbL attribution. |

Do NOT use commercial archives or third-party feeds that forbid rehosting (e.g. paid UK price archives, PetrolPrices feeds).

## UK price history strategy (do all three)
1. **Test the API's history depth first.** Call the incremental prices endpoint with "since 2026-02-02". Record exactly what comes back (latest price per station only, or a full change log?). Document the result in `docs/history-depth.md`. If it returns a change log, backfill everything.
2. **Recover old snapshots.** Check the Wayback Machine for archived copies of the Fuel Finder CSV and of retailer JSON feeds from the earlier voluntary CMA scheme (2023–Feb 2026). Ingest whatever exists, clearly flagged as patchy.
3. **Become the archive.** From day one, poll the API frequently and append every observed price change to an immutable raw store. Publish the cleaned history as an open dataset (GitHub release and/or Hugging Face) with a data dictionary, licence and attribution, updated regularly.

Analysis that needs history runs on **Germany's Tankerkönig data from day one** (the "Lab"), and the same code runs on UK data as the archive grows.

## Fuel Finder terms for public display (comply strictly)
- Displayed prices must stay closely in sync with the Fuel Finder database (guidance refers to updating within about 5 minutes so prices shown don't differ materially). The public site's *current price* layer must therefore refresh far more often than daily — target every 5–15 minutes, within fair-use and caching guidance.
- Never amend, hide or selectively present prices to promote or hinder any trader.
- Include a "Report a discrepancy" link (or forward complaints to the aggregator).
- Attribute: "Contains public sector information licensed under the Open Government Licence v3.0."
- Re-check the current developer terms before launch and record them in `docs/compliance.md`.

## Architecture
```
UK-hosted collector (small VM or container in Azure UK South — required because Fuel Finder only answers UK IPs)
  → polls Fuel Finder (incremental) every few minutes; other sources on their own schedules
  → raw, append-only Parquet in object storage (immutable; never overwritten)
  → dbt (DuckDB; optionally MotherDuck)
       staging      : rename, type, dedupe, standardise units
       intermediate : station dimension, price change events, local markets, tariff normalisation
       marts        : price_of_a_mile, market_signals, competition, mile_index, zoom-level aggregates
       snapshots    : station price history (SCD2)
       tests        : uniqueness, not-null, accepted ranges, freshness
  → exports: (a) small "current prices" JSON refreshed every 5–15 min,
             (b) PMTiles / Parquet per zoom level rebuilt daily,
             (c) the open archive dataset
  → uploaded to Cloudflare R2 (no egress fees) and served via CDN
Front end: static site on Cloudflare Pages (or similar), reading data files from R2.
GitHub Actions: CI (tests, dbt build on sample data, lint), daily rebuilds, dataset releases — NOT the Fuel Finder collector (US runners get 403).
```
Front end stack: SvelteKit (static) + MapLibre GL + deck.gl for the map, D3 for panels/charts, DuckDB-WASM for in-browser queries (postcode lookup, "Ask MileMap").
Basemap: self-hosted vector basemap (e.g. Protomaps PMTiles on R2) so map tiles don't incur per-view charges at scale.
Python managed with `uv`; dbt-duckdb; Node for the front end.

### Scale requirements
- Everything a visitor loads is a static file from a CDN; no database queries per visit.
- Split data by zoom level and tile; never ship the full dataset to the browser.
- First load under ~2 MB on mobile; interactive within a few seconds on 4G.
- Set long cache headers on versioned files, short ones on the current-prices file.
- Load-test the CDN path before launch.

## Known data-cleaning rules (dbt models + tests)
- Prices outside 100–300p/litre are treated as data-entry errors (flag, exclude from analytics, keep in raw).
- The Fuel Finder "brand"/organisation fields don't always identify the price-setter, and some fields have been removed in API changes — build a brand/operator mapping table and tolerate missing fields.
- Supermarket flags can be wrong — don't rely on them blindly.
- Late-arriving prices: incremental loads keyed on station + fuel type + effective timestamp; dedupe.
- EV tariffs come per kWh, per minute, with connection/idle fees — normalise to comparable pence per kWh for a defined standard session (document assumptions).
- All timestamps stored in UTC; convert to Europe/London for display. Test DST transitions explicitly.
- The collector must validate each run and refuse to publish if checks fail (keep the previous good data live).
- Never overwrite raw data.

## Analysis definitions
- **Price of a mile** = fuel price ÷ efficiency. Defaults: typical mpg for petrol/diesel (UK gallon = 4.546 L); typical kWh/mile for EVs. Users can change efficiency. Document every default.
- **Local market** = all stations within a 10-minute drive (pre-computed isochrones).
- **Price change event** = a change in price for station + fuel type at its effective timestamp.
- **Leader/follower** = within a local market, which station changes first and how quickly others match (direction + magnitude within a time window). Leader score per station.
- **Lockstep signal** = share of a market's price changes that happen near-simultaneously, vs a baseline.
- **Price cycles** = sawtooth patterns (sharp rise, gradual undercutting).
- **Rockets and feathers** = asymmetry of pump-price response to Brent rises vs falls (national UK from DESNZ weekly; local once history exists; German stations in the Lab).
- **Snapshot metrics (work from day one)**: price dispersion within a local market, motorway premium, competition score (independent operators within local market), fuel deserts, supermarket vs branded gap.
- **Mile Index** = daily basket index, base 100 = 2 Feb 2026, personas: petrol commuter, EV with driveway, EV without driveway (public charging), rural driver.
- **Minutes of work** = minutes at local median hourly pay to drive 100 miles.
History-based UK signals must show how many days of data they use and stay labelled "collecting data" until there is enough.

## The map — semantic zoom
1. **UK**: 3D glowing columns by region (height = price, colour cyan→amber). Play button animates collected history ("the breathing map"). National overlay with today's price of a mile.
2. **Regions / counties**: local authorities; hover cards (price of a mile, competition, EV count); urban/rural tint.
3. **Towns / local markets**: drive-time zones; price ripples from leaders to followers; fuel deserts and "lonely towers" stand out.
4. **Streets / stations**: each forecourt as a glowing LED price sign; each charger as a cyan plug with real p/kWh. Click → side panel: price history ("heartbeat"), behaviour type, who it leads/follows; chargers show tariff, availability.

Controls: postcode search (fly-to + local card), layers (Pump · Plug · Competition · Price of a Mile · Carbon), time slider, split view (compare two places), "Is it worth the drive?" detour calculator, "Ask MileMap" question box, a **Lab** panel for the Germany analysis, and a **Data** panel linking to the open archive.

## Design language — "the forecourt at night"
- Dark background (canopy at night); theme tokens on `:root`; explicit body background.
- Sodium-amber for petrol/diesel, electric cyan for EVs, used consistently.
- Prices as glowing seven-segment LED digits (free/OFL font).
- Subtle glow and motion; smooth on a mid-range phone; respects reduced-motion.
- Accessible: never colour alone; keyboard navigable; screen-reader labels on key numbers.

## Ethics & framing
- "Price leadership" and "coordination signals", never "cartel" or "collusion".
- Methodology page: every metric, assumption, limitation, and what signals do and do not prove.
- Attribute all sources (OGL v3.0, CC BY / CC BY-NC-SA for Tankerkönig, ODbL, etc.) in the footer, README and dataset card.

## Delivery phases
**Phase 1 — data foundation (starts the archive clock)**
- Repo scaffold; history-depth test (strategy step 1); UK-hosted collector for Fuel Finder; CSV fallback; raw immutable storage; DESNZ weekly + Brent ingestion; Tankerkönig ingestion; dbt staging + tests; CI.
**Phase 2 — the public map (price of a mile)**
- Station dimension, snapshot metrics, zoom-level exports, map levels 1–4, postcode search, station panel, current-prices file refreshed every 5–15 min, Cloudflare hosting, compliance items.
**Phase 3 — EV and household**
- OCPI chargers + tariff normalisation, Plug layer, Octopus history, Mile Index + personas, driveway divide, minutes of work, split view, worth-the-drive tool.
**Phase 4 — market signals**
- Lab on German data (leader/follower, ripples, lockstep, cycles, rockets-and-feathers); national UK rockets-and-feathers; switch on UK local signals as the archive grows; methodology page.
**Phase 5 — open archive, polish & launch**
- Wayback recovery (strategy step 2); publish the open dataset with data card; Ask MileMap; fly-through intro; performance and load testing; README hero GIF; launch writeup.

## Definition of done (per phase)
- Collector and pipelines run green; all dbt tests pass; freshness badge in README.
- Every metric on the map documented on the Methodology page.
- Mobile performance targets met; no horizontal scroll at phone width.
- README updated: what's new, screenshots/GIF, how to run locally.

## Working rules for agents
- Secrets (Fuel Finder credentials, cloud keys) only in environment variables / secret stores; never commit them.
- Small, reviewable commits; one feature per branch/PR.
- Write dbt tests alongside every new model.
- Don't add features outside the current phase without asking.
- Official APIs and open data only; respect each source's licence and terms.
- Ask before adding any paid service or anything that could incur ongoing cost.
